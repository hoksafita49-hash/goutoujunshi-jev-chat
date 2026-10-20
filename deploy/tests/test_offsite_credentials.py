"""Exercise credential boundaries without contacting OSS or handling real secrets."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

OFFSITE = Path(__file__).resolve().parents[1] / "offsite"
HELPER = OFFSITE / "configure_offsite.py"
sys.path.insert(0, str(OFFSITE))
if HELPER.exists():
    spec = importlib.util.spec_from_file_location("configure_offsite", HELPER)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)


def supplied(**changes):
    value = {
        "access_key_id": "TESTONLYACCESSKEY123",
        "access_key_secret": "test-only-access-secret-123456789",
        "password": "test-only-independent-repository-password-123456789",
    }
    value.update(changes)
    return value


class OffsiteCredentialTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(HELPER.exists(), "secure offsite credential setter is not implemented")
        # Keep temporary files in the explicitly writable checkout on Windows.
        self.directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "private" / "config.json"
        self.lock = Path(self.directory.name) / "offsite.lock"
        self.addCleanup(patch.stopall)
        # Linux flock and Linux owner/mode enforcement belong to offsite_common;
        # these tests exercise the setter's real validation and atomic writes.
        patch.object(helper, "FileLock", lambda path: contextlib.nullcontext()).start()
        original_load = helper.load_config
        patch.object(helper, "load_config", lambda path: original_load(path, file_check=lambda path: None)).start()
        if os.name != "posix":
            patch.object(helper, "prepare_private_directory", lambda path: path.mkdir(parents=True, exist_ok=True)).start()

    def parse(self, payload=None):
        return helper.parse_input(json.dumps(payload or supplied()).encode("utf-8"))

    def existing(self):
        self.config.parent.mkdir(mode=0o700)
        original = json.dumps(self.parse(), indent=4).encode("utf-8") + b"\n"
        self.config.write_bytes(original)
        self.config.chmod(0o600)
        return original

    def configure(self, candidate, runner):
        return helper.configure_credentials(self.config, candidate, runner=runner, lock_path=self.lock)

    def test_invalid_fields_lengths_controls_and_password_reuse_are_rejected(self):
        cases = [
            supplied(access_key_id=""), supplied(access_key_id="not a key"),
            supplied(access_key_secret="short"), supplied(password="x" * 15),
            supplied(password="x" * 16 + "\n"), supplied(password="x" * 16 + "\r"),
            supplied(password="x" * 16 + "\0"), supplied(access_key_secret="x" * 32 + "\n"),
            supplied(password=supplied()["access_key_secret"]),
            {**supplied(), "bucket": "other-bucket"},
        ]
        for case in cases:
            with self.subTest(case=list(case)):
                with self.assertRaises(helper.OffsiteError) as error:
                    helper.parse_input(json.dumps(case).encode())
                self.assertEqual(error.exception.code, "invalid_input")
                self.assertNotIn(supplied()["access_key_secret"], str(error.exception))

    def test_oversized_malformed_and_duplicate_json_are_rejected(self):
        for raw in (b"x" * 16385, b"{", b"[]", b"\xff", b'{"password":"a","password":"b"}'):
            with self.subTest(length=len(raw)):
                with self.assertRaises(helper.OffsiteError) as error:
                    helper.parse_input(raw)
                self.assertEqual(error.exception.code, "invalid_input")

    def test_minimum_16_character_password_is_accepted_without_normalization(self):
        password = "x" * 16
        parsed = self.parse(supplied(password=password))
        self.assertEqual(parsed["password"], password)

    def test_empty_prefix_must_be_successfully_listed_before_init_and_write(self):
        calls = []
        def runner(args, candidate, timeout=120):
            calls.append(args)
            self.assertFalse(self.config.exists(), "credentials were saved before repository validation")
            if args[:2] == ["rclone", "lsf"]:
                self.assertEqual(args[2], "goutouoss:zovoeh-backup-20261021/goutou-v1")
                self.assertIn("--recursive", args)
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:2] == ["restic", "init"]:
                return subprocess.CompletedProcess(args, 0, "repository initialized", "")
            self.fail("unexpected repository operation")
        result = self.configure(self.parse(), runner)
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved["bucket"], "zovoeh-backup-20261021")
        self.assertEqual(saved["endpoint"], "https://oss-cn-beijing.aliyuncs.com")
        self.assertEqual(saved["prefix"], "goutou-v1")
        self.assertEqual(saved["version"], 1)
        self.assertEqual(result["status"], "configured")
        self.assertEqual(len(calls), 2)
        for args in calls:
            self.assertNotIn(saved["password"], " ".join(args))
            self.assertNotIn(saved["access_key_secret"], " ".join(args))
        self.assertEqual(list(self.config.parent.glob(".offsite-*")), [])

    def test_nonempty_prefix_opens_existing_repository_without_initializing(self):
        original = self.existing()
        candidate = self.parse(supplied(access_key_secret="replacement-test-secret-123456789"))
        calls = []
        def runner(args, config, timeout=120):
            calls.append(args)
            self.assertEqual(self.config.read_bytes(), original)
            if args[:2] == ["rclone", "lsf"]:
                return subprocess.CompletedProcess(args, 0, "config\nkeys/key-id\n", "")
            self.assertEqual(args, ["restic", "cat", "config"])
            return subprocess.CompletedProcess(args, 0, '{"version":2}', "")
        self.configure(candidate, runner)
        self.assertEqual(json.loads(self.config.read_text())["access_key_secret"], "replacement-test-secret-123456789")
        self.assertEqual(len(calls), 2)

    def test_listing_or_repository_failure_preserves_previous_config_exactly(self):
        original = self.existing()
        candidate = self.parse(supplied(access_key_secret="replacement-test-secret-123456789"))
        for failed_stage in ("list", "open"):
            with self.subTest(stage=failed_stage):
                def runner(args, config, timeout=120):
                    if args[:2] == ["rclone", "lsf"]:
                        if failed_stage == "list":
                            raise helper.OffsiteError("tool_failed")
                        return subprocess.CompletedProcess(args, 0, "config\n", "")
                    raise helper.OffsiteError("tool_failed")
                with self.assertRaises(helper.OffsiteError):
                    self.configure(candidate, runner)
                self.assertEqual(self.config.read_bytes(), original)

    def test_initialization_failure_does_not_create_local_configuration(self):
        def runner(args, config, timeout=120):
            if args[:2] == ["rclone", "lsf"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            raise helper.OffsiteError("tool_failed")
        with self.assertRaises(helper.OffsiteError) as error:
            self.configure(self.parse(), runner)
        self.assertEqual(error.exception.code, "repository_init_failed")
        self.assertFalse(self.config.exists())

    def test_changed_repository_password_is_rejected_before_any_network_call(self):
        original = self.existing()
        def forbidden(*args, **kwargs):
            self.fail("changing an existing repository password made a cloud request")
        with self.assertRaises(helper.OffsiteError) as error:
            self.configure(self.parse(supplied(password="another-test-repository-password-123456789")), forbidden)
        self.assertEqual(error.exception.code, "password_change_forbidden")
        self.assertEqual(self.config.read_bytes(), original)

    def test_nonzero_runner_results_are_never_treated_as_empty_success(self):
        original = self.existing()
        def runner(args, config, timeout=120):
            return subprocess.CompletedProcess(args, 1, "", "TESTONLY provider error")
        with self.assertRaises(helper.OffsiteError):
            self.configure(self.parse(), runner)
        self.assertEqual(self.config.read_bytes(), original)

    def test_previously_configured_empty_repository_is_not_silently_reinitialized(self):
        original = self.existing()
        def runner(args, config, timeout=120):
            if args[:2] == ["rclone", "lsf"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            self.fail("a previously configured repository must never be silently recreated")
        with self.assertRaises(helper.OffsiteError) as error:
            self.configure(self.parse(), runner)
        self.assertEqual(error.exception.code, "repository_missing")
        self.assertEqual(self.config.read_bytes(), original)

    def test_cli_limits_input_read_and_rejects_oversized_input_before_network(self):
        class BoundedInput(io.BytesIO):
            def read(self, size=-1):
                self.requested_size = size
                return super().read(size)
        source = BoundedInput(b"x" * 20000)
        output = io.StringIO()
        with patch.object(helper, "require_root"), patch.object(helper.sys, "stdin", io.TextIOWrapper(source, encoding="utf-8")), patch.object(helper, "configure_credentials", side_effect=AssertionError("network reached")), contextlib.redirect_stdout(output):
            code = helper.main([])
        self.assertEqual(source.requested_size, 16385)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "error", "code": "invalid_input"})

    def test_check_reports_only_safe_status_without_cloud_requests_or_write(self):
        original = self.existing()
        with patch.object(helper.shutil, "which", side_effect=lambda name: "/usr/bin/" + name):
            result = helper.check_status(self.config)
        self.assertTrue(result["configured"])
        self.assertTrue(result["tools_available"])
        self.assertEqual(result["bucket"], "zovoeh-backup-20261021")
        self.assertEqual(self.config.read_bytes(), original)
        rendered = json.dumps(result)
        for secret in supplied().values():
            self.assertNotIn(secret, rendered)

    def test_cli_unexpected_exception_and_provider_details_are_redacted(self):
        secrets = supplied()
        output = io.StringIO()
        with patch.object(helper, "require_root"), patch.object(helper, "configure_credentials", side_effect=RuntimeError(json.dumps(secrets))), patch.object(helper.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(secrets).encode()), encoding="utf-8")), contextlib.redirect_stdout(output):
            exit_code = helper.main([])
        self.assertEqual(exit_code, 1)
        response = json.loads(output.getvalue())
        self.assertEqual(response, {"status": "error", "code": "configuration_failed"})
        for secret in secrets.values():
            self.assertNotIn(secret, output.getvalue())


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "requires Windows PowerShell")
class WindowsTransportTests(unittest.TestCase):
    def test_local_csv_import_accepts_one_account_and_rejects_unsafe_input_without_echo(self):
        wrapper = Path(__file__).resolve().parents[1] / "windows" / "configure-offsite-backup.ps1"
        command = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'wrapper_parse_failed' }
foreach ($name in @('ConvertFrom-AccessKeyCsv', 'Read-BoundedAccessKeyCsv')) {
    $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if ($null -eq $definition) { throw ('missing_csv_function:' + $name) }
    Invoke-Expression $definition.Extent.Text
}
$id = 'TESTONLYKEY123456789012345678901234'
$secret = 'test-only-secret-1234567890123456789'
$header = [char]0xfeff + '" Access_Key-ID ","Access Key Secret","DisplayName"'
$valid = $header + "`r`n" + '"  ' + $id + '  ","  ' + $secret + '  ","ignored, display name"'
$parsed = ConvertFrom-AccessKeyCsv $valid
if ($parsed.access_key_id -cne $id -or $parsed.access_key_secret -cne $secret -or $parsed.Count -ne 2) { throw 'csv_values_mismatch' }
$cases = @(
    '',
    'UserName,Password',
    ('AccessKey ID,DisplayName' + "`n" + $id + ',ignored'),
    ('AccessKey ID,AccessKey Secret' + "`n" + 'bad key,' + $secret),
    ('AccessKey ID,AccessKey Secret' + "`n" + $id + ',short'),
    ('AccessKey ID,AccessKey Secret' + "`n" + $id + ',' + $secret + "`n" + $id + ',' + $secret),
    ('AccessKey ID,Access_Key_ID,AccessKey Secret' + "`n" + $id + ',' + $id + ',' + $secret),
    ('UnexpectedID,UnexpectedSecret' + "`n" + $id + ',' + $secret),
    ('x' * 65537)
)
foreach ($case in $cases) {
    $rejected = $false
    try { [void](ConvertFrom-AccessKeyCsv $case) } catch {
        $rejected = $true
        if ($_.Exception.Message -notin @('csv_invalid', 'csv_too_large')) { throw 'csv_error_not_redacted' }
        if ($_.Exception.Message.Contains($id) -or $_.Exception.Message.Contains($secret)) { throw 'csv_error_leaked' }
    }
    if (-not $rejected) { throw 'unsafe_csv_accepted' }
}
$fixture = $args[1]
[IO.File]::WriteAllText($fixture, $valid, (New-Object Text.UTF8Encoding($true)))
$loaded = Read-BoundedAccessKeyCsv $fixture
if ($loaded.access_key_id -cne $id -or $loaded.access_key_secret -cne $secret) { throw 'csv_file_mismatch' }
[IO.File]::WriteAllBytes($fixture, (New-Object byte[] 65537))
$rejected = $false
try { [void](Read-BoundedAccessKeyCsv $fixture) } catch {
    $rejected = $true
    if ($_.Exception.Message -ne 'csv_too_large') { throw 'csv_file_error_not_redacted' }
}
if (-not $rejected) { throw 'oversize_csv_file_accepted' }
[IO.File]::WriteAllBytes($fixture, [byte[]]@(255))
$rejected = $false
try { [void](Read-BoundedAccessKeyCsv $fixture) } catch {
    $rejected = $true
    if ($_.Exception.Message -ne 'csv_invalid') { throw 'csv_encoding_error_not_redacted' }
}
if (-not $rejected) { throw 'invalid_utf8_csv_accepted' }
[Console]::WriteLine('local_csv_import_verified')
'''
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            probe = Path(directory) / "csv-test.ps1"
            fixture = Path(directory) / "synthetic-only.csv"
            probe.write_text(command, encoding="utf-8-sig")
            result = subprocess.run(
                [shutil.which("powershell.exe"), "-NoLogo", "-NoProfile", "-File", str(probe), str(wrapper), str(fixture)],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local_csv_import_verified", result.stdout)

    def test_hidden_inputs_retry_only_invalid_field_without_leaking_or_normalizing_password(self):
        wrapper = Path(__file__).resolve().parents[1] / "windows" / "configure-offsite-backup.ps1"
        command = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'wrapper_parse_failed' }
foreach ($name in @('Convert-HiddenValue', 'Read-ValidatedHiddenInput')) {
    $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if ($null -eq $definition) { throw ('missing_input_function:' + $name) }
    Invoke-Expression $definition.Extent.Text
}
$script:answers = New-Object 'Collections.Generic.Queue[string]'
$script:prompts = New-Object 'Collections.Generic.List[string]'
$script:messages = New-Object 'Collections.Generic.List[string]'
$id = 'TESTONLYKEY123456789012345678901234'
$secret = 'test-only-secret-1234567890123456789'
$password = '  preserved password ' + [char]0x4e2d + [char]0x6587 + ' 123456789  '
foreach ($answer in @('AccessKey ID,INVALID', ('  ' + $id + '  '), 'AccessKey Secret=INVALID', ('  ' + $secret + '  '), 'short', $id, $secret, $password, 'wrong confirmation', $password)) {
    $script:answers.Enqueue($answer)
}
function Read-Host([string]$Prompt, [switch]$AsSecureString) {
    if (-not $AsSecureString) { throw 'input_was_not_hidden' }
    $script:prompts.Add($Prompt)
    if (-not $script:answers.Count) { throw 'unexpected_reprompt' }
    $hidden = New-Object Security.SecureString
    foreach ($character in $script:answers.Dequeue().ToCharArray()) { $hidden.AppendChar($character) }
    $hidden.MakeReadOnly()
    return $hidden
}
function Write-Host([string]$Object, [object]$ForegroundColor) { $script:messages.Add($Object) }
$actualId = Read-ValidatedHiddenInput -Kind id
$actualSecret = Read-ValidatedHiddenInput -Kind secret
$actualPassword = Read-ValidatedHiddenInput -Kind password -Disallowed @($actualId, $actualSecret)
$actualConfirmation = Read-ValidatedHiddenInput -Kind confirmation -Compare $actualPassword
if ($actualId -cne $id -or $actualSecret -cne $secret -or $actualPassword -cne $password -or $actualConfirmation -cne $password) { throw 'input_value_changed' }
if ($script:answers.Count -ne 0 -or $script:prompts.Count -ne 10) { throw 'wrong_retry_count' }
if (@($script:prompts | Where-Object { $_ -like 'RAM AccessKey ID*' }).Count -ne 2) { throw 'id_reprompted_after_acceptance' }
if (@($script:prompts | Where-Object { $_ -like 'RAM AccessKey Secret*' }).Count -ne 2) { throw 'secret_reprompted_after_acceptance' }
$rendered = $script:messages -join "`n"
foreach ($value in @($id, $secret, $password, 'AccessKey ID,INVALID', 'AccessKey Secret=INVALID', 'wrong confirmation')) {
    if ($rendered.Contains($value)) { throw 'input_leaked_in_message' }
}
if (-not $rendered.Contains('ID') -or -not $rendered.Contains('Secret')) { throw 'field_error_not_specific' }
$script:answers.Enqueue('x' * 15)
$script:answers.Enqueue('x' * 16)
$boundaryPassword = Read-ValidatedHiddenInput -Kind password
if ($boundaryPassword -cne ('x' * 16) -or $script:answers.Count -ne 0 -or $script:prompts.Count -ne 12) { throw 'password_minimum_boundary_failed' }
[Console]::WriteLine('hidden_field_retry_verified')
'''
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            probe = Path(directory) / "input-test.ps1"
            probe.write_text(command, encoding="utf-8-sig")
            result = subprocess.run(
                [shutil.which("powershell.exe"), "-NoLogo", "-NoProfile", "-File", str(probe), str(wrapper)],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hidden_field_retry_verified", result.stdout)

    def test_windows_powershell_transports_utf8_without_bom(self):
        """Exercise the real helper functions and .NET Framework redirected pipe."""
        wrapper = Path(__file__).resolve().parents[1] / "windows" / "configure-offsite-backup.ps1"
        command = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'wrapper_parse_failed' }
foreach ($name in @('New-RedirectedProcessStartInfo', 'Write-Utf8ProcessInput')) {
    $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if ($null -eq $definition) { throw ('missing_transport_function:' + $name) }
    Invoke-Expression $definition.Extent.Text
}
$echoCode = '$ProgressPreference = "SilentlyContinue"; $raw = [Console]::OpenStandardInput(); $data = New-Object IO.MemoryStream; $raw.CopyTo($data); $bytes = $data.ToArray(); [Console]::OpenStandardOutput().Write($bytes, 0, $bytes.Length)'
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($echoCode))
$start = New-RedirectedProcessStartInfo (Join-Path $PSHOME 'powershell.exe')
$start.Arguments = '-NoLogo -NoProfile -EncodedCommand ' + $encoded
$process = New-Object Diagnostics.Process
$process.StartInfo = $start
try {
    [void]$process.Start()
    $outputTask = $process.StandardOutput.ReadToEndAsync()
    $errorTask = $process.StandardError.ReadToEndAsync()
    $sample = '{"sample":"' + [char]0x4e2d + [char]0x6587 + [char]0xd83d + [char]0xde42 + '"}'
    Write-Utf8ProcessInput $process $sample
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(15000)) { $process.Kill(); throw 'local_pipe_timeout' }
    $received = $outputTask.GetAwaiter().GetResult()
    $childError = $errorTask.GetAwaiter().GetResult()
    if ($process.ExitCode -ne 0 -or $childError.Length -ne 0) { throw 'local_pipe_failed' }
    if ($received -cne $sample) { throw 'utf8_wire_mismatch_or_bom' }
    [Console]::WriteLine('utf8_no_bom_verified')
} finally { $process.Dispose() }
'''
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            probe = Path(directory) / "transport-test.ps1"
            probe.write_text(command, encoding="utf-8-sig")
            result = subprocess.run(
                [shutil.which("powershell.exe"), "-NoLogo", "-NoProfile", "-File", str(probe), str(wrapper)],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("utf8_no_bom_verified", result.stdout)


if __name__ == "__main__":
    unittest.main()
