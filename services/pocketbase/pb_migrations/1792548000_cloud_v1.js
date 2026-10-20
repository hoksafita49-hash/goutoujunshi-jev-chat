migrate(
  (app) => {
    const schemas = {
      cloud_meta: { name: "text", epoch: "text", head: "number" },
      cloud_devices: {
        owner: "text",
        name: "text",
        platform: "text",
        secret_hash: "text",
        expires_at: "text",
        revoked_at: "text",
      },
      cloud_entities: {
        owner: "text",
        entity_type: "text",
        uid: "text",
        version: "number",
        created_at: "text",
        updated_at: "text",
        deleted_at: "text",
        payload: "json",
      },
      cloud_changes: {
        owner: "text",
        entity_type: "text",
        uid: "text",
        version: "number",
        seq: "number",
        record: "json",
      },
      cloud_receipts: {
        owner: "text",
        device_id: "text",
        op_id: "text",
        digest: "text",
        result: "json",
      },
      cloud_jobs: {
        owner: "text",
        uid: "text",
        request_id: "text",
        digest: "text",
        kind: "text",
        status: "text",
        conversation_uid: "text",
        context_revision: "number",
        input: "json",
        result: "json",
        error_code: "text",
        claim_token: "text",
        created_at: "text",
        updated_at: "text",
      },
    };
    const indexes = {
      cloud_meta: ["CREATE UNIQUE INDEX cloud_meta_name ON cloud_meta (name)"],
      cloud_devices: [
        "CREATE INDEX cloud_devices_owner ON cloud_devices (owner)",
      ],
      cloud_entities: [
        "CREATE UNIQUE INDEX cloud_entity_uid ON cloud_entities (owner, entity_type, uid)",
      ],
      cloud_changes: [
        "CREATE UNIQUE INDEX cloud_change_seq ON cloud_changes (seq)",
        "CREATE INDEX cloud_owner_seq ON cloud_changes (owner, seq)",
        "CREATE INDEX cloud_history ON cloud_changes (owner, entity_type, uid, version)",
      ],
      cloud_receipts: [
        "CREATE UNIQUE INDEX cloud_receipt_op ON cloud_receipts (owner, device_id, op_id)",
      ],
      cloud_jobs: [
        "CREATE UNIQUE INDEX cloud_job_request ON cloud_jobs (owner, request_id)",
        "CREATE UNIQUE INDEX cloud_job_uid ON cloud_jobs (owner, uid)",
        "CREATE INDEX cloud_job_status ON cloud_jobs (status, created_at)",
      ],
    };
    const users = new Collection({
      type: "auth",
      name: "cloud_users",
      listRule: null,
      viewRule: null,
      createRule: null,
      updateRule: null,
      deleteRule: null,
      authRule: "",
      passwordAuth: { enabled: true, identityFields: ["email"] },
      authToken: { duration: 2592000 },
    });
    app.save(users);
    for (const name in schemas) {
      const fields = [];
      for (const field in schemas[name]) {
        const type = schemas[name][field];
        const definition = { name: field, type: type };
        if (type === "text")
          definition.max = field === "secret_hash" ? 128 : 512;
        if (type === "json") definition.maxSize = 2097152;
        fields.push(definition);
      }
      app.save(
        new Collection({
          type: "base",
          name: name,
          fields: fields,
          listRule: null,
          viewRule: null,
          createRule: null,
          updateRule: null,
          deleteRule: null,
          indexes: indexes[name],
        }),
      );
    }
    const meta = new Record(app.findCollectionByNameOrId("cloud_meta"));
    meta.set("name", "primary");
    const hex = $security.randomStringWithAlphabet(32, "0123456789abcdef");
    meta.set(
      "epoch",
      hex.slice(0, 8) +
        "-" +
        hex.slice(8, 12) +
        "-" +
        hex.slice(12, 16) +
        "-" +
        hex.slice(16, 20) +
        "-" +
        hex.slice(20),
    );
    meta.set("head", 0);
    app.save(meta);
  },
  (app) => {
    throw new Error(
      "Cloud v1 downgrade requires an explicit verified database restore",
    );
  },
);
