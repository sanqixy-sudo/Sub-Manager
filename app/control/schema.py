def migrate(conn):
    script = """
    CREATE TABLE IF NOT EXISTS proxy_servers (
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, host TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 1, bootstrap_hash TEXT, bootstrap_expires TEXT,
      agent_hash TEXT UNIQUE, instance_id TEXT, last_seen TEXT, applied_revision TEXT,
      runtime_status TEXT NOT NULL DEFAULT 'offline', error_code TEXT,
      metrics TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS proxy_nodes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      server_id INTEGER NOT NULL REFERENCES proxy_servers(id),
      name TEXT NOT NULL, protocol TEXT NOT NULL, port INTEGER NOT NULL,
      server_name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
      next_node_id INTEGER REFERENCES proxy_nodes(id),
      multiplier REAL NOT NULL DEFAULT 1,
      secrets TEXT NOT NULL, cert_fingerprint TEXT NOT NULL, cert_expires TEXT NOT NULL,
      created_at TEXT NOT NULL, UNIQUE(server_id,port)
    );
    CREATE TABLE IF NOT EXISTS proxy_users (
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
      subscription_hash TEXT NOT NULL UNIQUE, subscription_secret TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS proxy_sessions (
      token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES proxy_users(id) ON DELETE CASCADE,
      expires_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS proxy_packages (
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
      quota_bytes INTEGER NOT NULL DEFAULT 0, cycle_days INTEGER NOT NULL DEFAULT 30,
      duration_days INTEGER NOT NULL DEFAULT 30, enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS proxy_package_nodes (
      package_id INTEGER NOT NULL REFERENCES proxy_packages(id) ON DELETE CASCADE,
      node_id INTEGER NOT NULL REFERENCES proxy_nodes(id), PRIMARY KEY(package_id,node_id)
    );
    CREATE TABLE IF NOT EXISTS proxy_assignments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES proxy_users(id),
      package_id INTEGER NOT NULL REFERENCES proxy_packages(id),
      enabled INTEGER NOT NULL DEFAULT 1, expires_at TEXT NOT NULL,
      cycle_start TEXT NOT NULL, baseline_up INTEGER NOT NULL DEFAULT 0,
      baseline_down INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS proxy_grants (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      assignment_id INTEGER NOT NULL REFERENCES proxy_assignments(id) ON DELETE CASCADE,
      node_id INTEGER NOT NULL REFERENCES proxy_nodes(id),
      secret TEXT NOT NULL, upload INTEGER NOT NULL DEFAULT 0, download INTEGER NOT NULL DEFAULT 0,
      UNIQUE(assignment_id,node_id)
    );
    CREATE TABLE IF NOT EXISTS proxy_counter_samples (
      server_id INTEGER NOT NULL REFERENCES proxy_servers(id),
      instance_id TEXT NOT NULL, grant_id INTEGER NOT NULL REFERENCES proxy_grants(id),
      upload INTEGER NOT NULL, download INTEGER NOT NULL,
      PRIMARY KEY(server_id,instance_id,grant_id)
    );
    CREATE TABLE IF NOT EXISTS proxy_traffic_daily (
      day TEXT NOT NULL, grant_id INTEGER NOT NULL REFERENCES proxy_grants(id),
      upload INTEGER NOT NULL DEFAULT 0, download INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(day,grant_id)
    );
    CREATE INDEX IF NOT EXISTS idx_proxy_grants_node ON proxy_grants(node_id);
    CREATE INDEX IF NOT EXISTS idx_proxy_assignments_user ON proxy_assignments(user_id);
    """
    for statement in script.split(';'):
        if statement.strip():
            conn.execute(statement)
