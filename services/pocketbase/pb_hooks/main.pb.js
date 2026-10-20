routerAdd(
  "POST",
  "/internal/cloud/{action}",
  (e) => {
    const cloud = require(__hooks + "/cloud.js");
    return cloud.dispatch(e);
  },
  $apis.bodyLimit(262144),
);
