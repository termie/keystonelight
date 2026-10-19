# Keystone Light Architecture & Code Decisions: Technical Interview Guide

This document analyzes the architecture, internal mechanics, and notable engineering decisions in the `keystonelight` codebase. It is designed to provide inspiration for technical interview questions spanning systems programming, WSGI micro-frameworks, event-driven concurrency in Python, database modeling trade-offs, API evolution and backward compatibility, security and cryptography, and distributed authentication middleware.

> **Note on Formatting:** In accordance with instructions, this file contains **no markdown links** to files. All paths and symbols are cited using plain-text code spans.

---

## Table of Contents
1. [System Architecture Context & Per-Directory Overview](#1-system-architecture-context--per-directory-overview)
2. [Deep Dives: Key Code Decisions & Interview Questions](#2-deep-dives-key-code-decisions--interview-questions)
   - [Decision 1: Manager Layer, Context Stripping & Dynamic Driver Delegation](#decision-1-manager-layer-context-stripping--dynamic-driver-delegation)
   - [Decision 2: Dual-Port Daemon Architecture & Eventlet Cooperative Multitasking](#decision-2-dual-port-daemon-architecture--eventlet-cooperative-multitasking)
   - [Decision 3: Hybrid Relational/Document Storage with JsonBlob & DictBase Emulation](#decision-3-hybrid-relationaldocument-storage-with-jsonblob--dictbase-emulation)
   - [Decision 4: Global In-Memory Singleton KVS & Dict Slicing Quirks](#decision-4-global-in-memory-singleton-kvs--dict-slicing-quirks)
   - [Decision 5: Non-Invasive Extension Routing & Diablo Backward Compatibility Hacks](#decision-5-non-invasive-extension-routing--diablo-backward-compatibility-hacks)
   - [Decision 6: AWS EC2/S3 Interoperability & Signature Verification Pitfalls](#decision-6-aws-ec2s3-interoperability--signature-verification-pitfalls)
   - [Decision 7: Redundant Dual-Roundtrip Auth Token Middleware & Delegated Auth](#decision-7-redundant-dual-roundtrip-auth-token-middleware--delegated-auth)
   - [Decision 8: Reflection-Based CLI Generation & Hardcoded Driver Migrations](#decision-8-reflection-based-cli-generation--hardcoded-driver-migrations)
3. [Per-File Summary & Codebase Index](#3-per-file-summary--codebase-index)
4. [Interview Question Quick-Reference Matrix](#4-interview-question-quick-reference-matrix)

---

## 1. System Architecture Context & Per-Directory Overview

Keystone Light is an OpenStack Identity service architecture developed to replace the complex Diablo-era implementation with a streamlined, modular, WSGI-driven platform. Rather than acting as a heavyweight identity store, Keystone Light is architected primarily as a lightweight "shim" capable of sitting in front of existing enterprise identity systems (such as LDAP or PAM) or managing users directly via pluggable storage backends.

```
                              +-------------------------+
                              |   Client HTTP Request   |
                              +------------+------------+
                                           |
                                  [Eventlet WSGI]
                                           |
                    +----------------------+----------------------+
                    | Port 5000 (Public)                          | Port 35357 (Admin)
                    v                                             v
        +-----------------------+                     +-----------------------+
        | Public WSGI Pipeline  |                     |  Admin WSGI Pipeline  |
        | - TokenAuthMiddleware |                     | - TokenAuthMiddleware |
        | - AdminTokenAuth      |                     | - AdminTokenAuth      |
        | - JsonBodyMiddleware  |                     | - JsonBodyMiddleware  |
        | - Ec2Extension Router |                     | - Ec2Extension Router |
        | - Public Router       |                     | - CrudExtension Router|
        +-----------+-----------+                     | - Admin Router        |
                    |                                 +-----------+-----------+
                    +----------------------+----------------------+
                                           |
                                           v
                              +-------------------------+
                              | Application Controllers |
                              | (Token, Tenant, User,   |
                              |  Role, Catalog, Ec2)    |
                              +------------+------------+
                                           |
                                           v
                              +-------------------------+
                              |      Manager Layer      |
                              | (Dynamic driver proxy   |
                              |  & context stripper)    |
                              +------------+------------+
                                           |
                    +----------------------+----------------------+
                    |                      |                      |
                    v                      v                      v
        +-----------------------+ +-----------------+ +-----------------------+
        | In-Memory KVS Driver  | | SQL ORM Driver  | | Linux PAM Driver      |
        | (Global DictKvs)      | | (SQLite/MySQL)  | | (System Auth)         |
        +-----------------------+ +-----------------+ +-----------------------+
```

### Core Architecture & Execution Flow
1. **Pipeline Ingestion & Context Extraction (`keystone/middleware/core.py`, `etc/keystone.conf`):** Incoming HTTP requests pass through Paste Deploy WSGI filter chains. `TokenAuthMiddleware` extracts `X-Auth-Token` into `openstack.context`, `AdminTokenAuthMiddleware` evaluates admin bypass tokens, and `JsonBodyMiddleware` unpacks deserialized JSON into `openstack.params`.
2. **Routing & Extension Composition (`keystone/common/wsgi.py`, `keystone/service.py`, `keystone/contrib/`):** Routes dispatch requests to controller actions. Extensions (`CrudExtension`, `Ec2Extension`) prepend their custom routes to the main WSGI router, forwarding unmatched paths to downstream applications via catch-all `{path_info:.*}` routes.
3. **Controller Orchestration & Admin Verification (`keystone/identity/core.py`, `keystone/service.py`):** Controllers authenticate users, enforce authorization checks (`assert_admin`), generate UUID tokens, and format output dictionaries to match OpenStack Identity API v2.0 specifications.
4. **Manager Proxy & Context Stripping (`keystone/common/manager.py`):** Controllers invoke services via `Manager` instances. The manager dynamically loads the configured driver class, intercepts method invocations via `__getattr__`, strips the request `context` parameter, and caches the generated wrapper function directly onto the manager instance.
5. **Pluggable Storage Abstraction (`keystone/identity/backends/`, `keystone/token/backends/`):** The driver performs persistence operations against an in-memory dictionary (`kvs.py`), a hybrid relational/document SQLAlchemy model with JSON blob storage (`sql.py`), or external authentication systems (`pam.py`).
6. **Downstream Service Enforcement (`keystone/middleware/auth_token.py`):** External OpenStack services (Nova, Glance, Swift) use Keystone's auth-token middleware to intercept client requests, query Keystone over HTTP to validate tokens, and decorate request headers (`X-User-Id`, `X-Tenant-Id`, `X-Roles`) before handing control to downstream services.

---

### Per-Directory Overview

- `bin/`: Executable entry-point scripts. Houses `bin/keystone-all` (starts the public and admin WSGI servers inside an Eventlet greenlet loop) and `bin/keystone-manage` (CLI operations such as `db_sync`).
- `etc/`: Configuration files and service catalog templates. Includes `etc/keystone.conf` (Paste Deploy pipeline definitions and driver declarations), `etc/default_catalog.templates` (string-interpolated service endpoint catalog definitions), and logging configurations.
- `keystone/`: Root application package. Contains top-level service routing (`keystone/service.py`), configuration schema registration (`keystone/config.py`), CLI command dispatch (`keystone/cli.py`), and test runners/harnesses (`keystone/test.py`).
- `keystone/catalog/`: Service Catalog subsystem. Provides controllers and managers enabling clients to discover API endpoints across regions and services.
- `keystone/catalog/backends/`: Storage drivers for endpoint catalogs, including in-memory KVS storage (`kvs.py`) and dynamic string-interpolated template file parsing (`templated.py`).
- `keystone/common/`: Shared framework infrastructure. Houses the custom WebOb/Routes WSGI micro-framework (`wsgi.py`), the dynamic Manager-Driver proxy bridge (`manager.py`), configuration parsing (`cfg.py`), in-memory key-value primitives (`kvs.py`), buffered HTTP socket monkey-patching (`bufferedhttp.py`), and cryptographic helpers (`utils.py`).
- `keystone/common/sql/`: Relational persistence layer using SQLAlchemy. Implements database engine lifecycles, session creation, `DictBase` model dictionary emulation, and the `JsonBlob` schemaless column type.
- `keystone/common/sql/migrate_repo/`: SQLAlchemy-migrate repository maintaining database version tracking and migration scripts (such as `001_add_initial_tables.py`).
- `keystone/contrib/`: Optional extensions augmenting core functionality without bloating the authentication core.
- `keystone/contrib/admin_crud/`: The OS-KSADM administrative CRUD extension, exposing RESTful management endpoints for users, tenants, roles, and services.
- `keystone/contrib/ec2/`: AWS EC2 credential compatibility extension. Bridges flat AWS access/secret keys with OpenStack multi-tenant accounts, validates HMAC signatures, and converts AWS requests into Keystone tokens.
- `keystone/contrib/ec2/backends/`: Persistence drivers (KVS and SQL) for EC2 credentials.
- `keystone/contrib/s3/`: AWS S3 authentication extension (`/s3tokens`), subclassing the EC2 controller to validate S3-style HMAC-SHA1 signatures.
- `keystone/identity/`: Core identity business logic. Defines the `Driver` interface, routing trees, and controllers for tenants, users, and roles.
- `keystone/identity/backends/`: Pluggable identity persistence backends: in-memory KVS (`kvs.py`), SQLAlchemy ORM (`sql.py`), and Linux PAM (`pam.py`).
- `keystone/middleware/`: WSGI middlewares. Contains internal request preprocessing filters (`core.py`) and external service integration shims (`auth_token.py`, `ec2_token.py`, `glance_auth_token.py`, `nova_auth_token.py`, `swift_auth.py`).
- `keystone/policy/`: Rule-based authorization engine. Contains the policy manager interface.
- `keystone/policy/backends/`: Policy drivers implementing rule evaluation, including `SimpleMatch` and `TrivialTrue`.
- `keystone/token/`: Token management subsystem. Defines token lifecycle contracts and data structures.
- `keystone/token/backends/`: Token storage drivers, deliberately implemented only as an in-memory KVS (`kvs.py`).
- `tests/`: Test suite containing unit tests, backend parity test mixins (`test_backend.py`), CLI tests, contract verification against Diablo documentation samples (`test_legacy_compat.py`), and live `keystoneclient` integration tests.
- `tools/`: Environment bootstrapping scripts (`install_venv.py`, `with_venv.sh`) and pip dependency manifests.
- `docs/`: Sphinx documentation sources, configuration (`conf.py`), man pages, and sequence diagrams.

---

## 2. Deep Dives: Key Code Decisions & Interview Questions

---

### Decision 1: Manager Layer, Context Stripping & Dynamic Driver Delegation

- **Relevant Files:**
  - `keystone/common/manager.py` (`Manager.__init__`, `Manager.__getattr__`)
  - `keystone/identity/core.py` (`Manager`)
  - `keystone/token/core.py` (`Manager`)
  - `keystone/catalog/core.py` (`Manager`)

#### The Code Decision
To decouple the HTTP-facing controller layer from pluggable backend drivers, Keystone introduces a `Manager` base class in `keystone/common/manager.py`. The manager dynamically imports driver classes from configuration strings and uses Python's `__getattr__` hook to intercept and forward method calls to the driver:

```python
class Manager(object):
    def __init__(self, driver_name):
        self.driver = utils.import_object(driver_name)

    def __getattr__(self, name):
        f = getattr(self.driver, name)

        @functools.wraps(f)
        def _wrapper(context, *args, **kw):
            return f(*args, **kw)
        setattr(self, name, _wrapper)
        return _wrapper
```

Notice two critical behaviors:
1. **Context Stripping:** The wrapper explicitly receives `context` as its first parameter and discards it before delegating to `f(*args, **kw)`.
2. **Method Caching via `setattr`:** Once a method is resolved through `__getattr__`, the wrapper function is attached directly to the `Manager` instance (`setattr(self, name, _wrapper)`). Subsequent invocations bypass `__getattr__` entirely.

#### Context & Tradeoffs
- **Advantages:**
  - Controllers interact with a stable, uniform API regardless of whether the backend is backed by an in-memory dict, a SQL database, or an LDAP directory.
  - Caching wrappers on `self` avoids repeated `__getattr__` lookups on hot code paths.
- **Vulnerabilities & Edge Cases:**
  - **Context Loss for Drivers:** By stripping `context`, backend drivers have no access to security credentials, tenant boundaries, or request IDs. This breaks auditing, multi-tenant database partitioning, and fine-grained authorization at the storage layer.
  - **Instance Mutation & Concurrency:** Mutating `self` via `setattr` during runtime method dispatch can lead to race conditions if multiple threads initialize or invoke methods concurrently before caching settles.
  - **Lack of Interface Enforcement:** Because the dispatch relies on dynamic attribute lookup rather than abstract base classes or protocols, driver omissions only surface as runtime `AttributeError` exceptions when a specific method is called.

#### Interview Questions & Discussion Angles
1. **Dynamic Proxying vs. Abstract Base Classes:**
   > *"What are the architectural trade-offs of using `__getattr__` dynamic method delegation and runtime caching via `setattr` versus explicit abstract base classes (`abc.ABCMeta`) or interface inheritance?"*
2. **Context Propagation in Service Architectures:**
   > *"The comment in `Manager.__getattr__` states: 'context is the first argument, we're going to strip that for now'. What architectural problems arise when storage layers are starved of request context (tracing IDs, user identity, tenant scope)?"*
3. **Python Runtime & Object Mutation:**
   > *"How does attaching dynamically generated closures to an instance using `setattr(self, name, _wrapper)` affect memory consumption, method rebinding, and introspection tools (like `inspect.getargspec`)?"*

---

### Decision 2: Dual-Port Daemon Architecture & Eventlet Cooperative Multitasking

- **Relevant Files:**
  - `bin/keystone-all` (`create_server`, `serve`, `main`)
  - `bin/keystone-manage` (`main`)
  - `keystone/common/wsgi.py` (`Server`, `WritableLogger`)

#### The Code Decision
In `bin/keystone-all`, Keystone hosts two distinct HTTP APIs—the Admin API (port 35357) and the Public/Main API (port 5000)—inside a single operating system process:

```python
def create_server(conf, name, port):
    app = deploy.loadapp('config:%s' % conf, name=name)
    return wsgi.Server(app, port)

def serve(*servers):
    for server in servers:
        server.start()
    for server in servers:
        try:
            server.wait()
        except greenlet.GreenletExit:
            pass

# In main:
servers.append(create_server(CONF.config_file[0], 'admin', int(options['admin_port'])))
servers.append(create_server(CONF.config_file[0], 'main', int(options['public_port'])))
serve(*servers)
```

In `keystone/common/wsgi.py`, Eventlet monkey-patching is applied selectively:
```python
import eventlet
import eventlet.wsgi
eventlet.patcher.monkey_patch(all=False, socket=True, time=True)
```
Additionally, `bin/keystone-all` contains a subtle bug in lines 14-16 when attempting to resolve its repository root:
```python
if os.path.exists(os.path.join(possible_topdir, 'keystone-all', '__init__.py')):
    sys.path.insert(0, possible_topdir)
```
It checks for `keystone-all/__init__.py` instead of `keystone/__init__.py` (which `bin/keystone-manage` correctly checks).

#### Context & Tradeoffs
- **Advantages:**
  - Hosting both endpoints in one process simplifies deployment topologies in resource-constrained environments.
  - Eventlet coroutines provide high concurrency for I/O-bound workloads without the memory overhead of thousands of OS threads.
- **Vulnerabilities & Edge Cases:**
  - **Shared Failure Domain:** A fatal crash, unhandled exception, or memory leak in the public API immediately kills administrative access, preventing operators from diagnosing or mitigating issues.
  - **Coroutiune Starvation:** If any backend driver executes synchronous, un-patched blocking operations (e.g. CPU-heavy bcrypt hashing or blocking database client calls), the entire Eventlet event loop freezes, stalling all greenlets across both public and admin ports.
  - **Partial Monkey Patching Hazards:** Patching only `socket` and `time` (`all=False`) can create subtle race conditions and deadlocks when third-party libraries use standard OS threads or file I/O.

#### Interview Questions & Discussion Angles
1. **Concurrency Models & Cooperative Multitasking:**
   > *"Keystone Light uses Eventlet's greenlet-based cooperative multitasking. How does coroutine-based scheduling differ from OS-preempted threading, and what happens to the service when a greenlet performs a CPU-intensive operation like bcrypt password hashing?"*
2. **Security & Process Isolation:**
   > *"What are the security and operational risks of co-locating administrative management endpoints and untrusted public client endpoints in the same OS process and event loop?"*
3. **Bug Hunting & Path Resolution:**
   > *"Review lines 11-17 of `bin/keystone-all` and compare them with `bin/keystone-manage`. Can you spot the bug in how `possible_topdir` is validated, and what are the operational consequences for developer checkouts?"*

---

### Decision 3: Hybrid Relational/Document Storage with JsonBlob & DictBase Emulation

- **Relevant Files:**
  - `keystone/common/sql/core.py` (`JsonBlob`, `DictBase`, `Base`)
  - `keystone/identity/backends/sql.py` (`User`, `Tenant`, `Metadata`)
  - `keystone/common/sql/migrate_repo/versions/001_add_initial_tables.py`

#### The Code Decision
To achieve flexibility across relational databases before native JSON column support existed in MySQL or PostgreSQL, Keystone Light created a hybrid relational/document model. Models declare primary keys and indexed lookup columns explicitly, but store all arbitrary or dynamic attributes in a serialized JSON text column:

```python
class JsonBlob(sql_types.TypeDecorator):
    impl = sql.Text

    def process_bind_param(self, value, dialect):
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        return json.loads(value)

class User(sql.ModelBase, sql.DictBase):
    __tablename__ = 'user'
    id = sql.Column(sql.String(64), primary_key=True)
    name = sql.Column(sql.String(64), unique=True)
    extra = sql.Column(sql.JsonBlob())

    @classmethod
    def from_dict(cls, user_dict):
        extra = {}
        for k, v in user_dict.copy().iteritems():
            if k not in ['id', 'name']:
                extra[k] = user_dict.pop(k)
        user_dict['extra'] = extra
        return cls(**user_dict)
```

To make SQLAlchemy models interchangeable with dictionary responses throughout controllers, models inherit from `DictBase`. However, `DictBase.__iter__` contains an insidious state-mutation flaw:
```python
class DictBase(object):
    def __iter__(self):
        self._i = iter(sqlalchemy.orm.object_mapper(self).columns)
        return self

    def next(self):
        n = self._i.next().name
        return n
```

#### Context & Tradeoffs
- **Advantages:**
  - Allows backends to accept and persist arbitrary user metadata (email, display names, phone numbers) without requiring database schema migrations for every new attribute.
  - `DictBase` allows SQLAlchemy ORM models to be passed directly to JSON serializers expecting dict interfaces.
- **Vulnerabilities & Edge Cases:**
  - **Query & Indexing Limitations:** Attributes stored in `extra` (such as passwords, enabled flags, or tenant associations) cannot be indexed or efficiently queried with SQL `WHERE` clauses.
  - **Iterator State Mutation Bug:** In `DictBase`, `__iter__` mutates `self._i` on the model instance and returns `self` instead of returning a separate generator or iterator. If two parts of the codebase iterate over the same model instance simultaneously, or if iteration is nested, they overwrite `self._i`, causing corrupted iterations or infinite loops!
  - **Irreversible Migrations:** In `001_add_initial_tables.py`, `upgrade` calls `create_all()`, while `downgrade` is implemented as `pass`, making automated rollbacks impossible.

#### Interview Questions & Discussion Angles
1. **Relational vs. Document Modeling:**
   > *"What are the trade-offs of storing extensible entity attributes inside a serialized JSON column in a relational database, particularly regarding indexing, schema evolution, and query efficiency?"*
2. **Python Iterator Protocol Flaws:**
   > *"Review `DictBase.__iter__` and `DictBase.next` in `keystone/common/sql/core.py`. Why is setting iterator state directly on `self` an anti-pattern, and how would you implement a re-entrant, thread-safe iterator?"*
3. **Database Migration Safety:**
   > *"Why is calling `metadata.create_all()` in versioned database migration scripts considered problematic for long-term schema evolution, and why must downgrade paths be tested?"*

---

### Decision 4: Global In-Memory Singleton KVS & Dict Slicing Quirks

- **Relevant Files:**
  - `keystone/common/kvs.py` (`DictKvs`, `Base`, `INMEMDB`)
  - `keystone/identity/backends/kvs.py` (`Identity`)
  - `keystone/token/backends/kvs.py` (`Token`)

#### The Code Decision
Keystone provides an in-memory key-value store driver designed for testing and simple deployments. In `keystone/common/kvs.py`, all KVS backend instances share a single module-level dictionary singleton by default:

```python
class DictKvs(dict):
    def set(self, key, value):
        if type(value) is type({}):
            self[key] = value.copy()
        else:
            self[key] = value[:]

    def delete(self, key):
        del self[key]

INMEMDB = DictKvs()

class Base(object):
    def __init__(self, db=None):
        if db is None:
            db = INMEMDB
        elif type(db) is type({}):
            db = DictKvs(db)
        self.db = db
```

In `keystone/identity/backends/kvs.py`, credential verification relies on raising assertions:
```python
if (not user_ref or not utils.check_password(password, user_ref.get('password'))):
    raise AssertionError('Invalid user / password')
if tenant_id and tenant_id not in user_ref['tenants']:
    raise AssertionError('Invalid tenant')
```

#### Context & Tradeoffs
- **Advantages:**
  - Instant bootstrapping with zero external database dependencies.
  - Allows unit tests to run in milliseconds entirely in memory.
- **Vulnerabilities & Edge Cases:**
  - **The Slicing Type Error:** In `DictKvs.set`, if `value` is not a dict, it attempts `self[key] = value[:]`. If `value` is an integer, boolean, or custom object, `value[:]` raises `TypeError: 'int' object is not subscriptable`.
  - **Shared State & Key Collisions:** Because `INMEMDB` is a shared global singleton across identity, token, catalog, and EC2 services, services must carefully namespace keys (e.g. `user-%s`, `tenant-%s`, `token-%s`) to avoid silent data overwrites.
  - **Control Flow via Assertions:** Raising `AssertionError` for invalid passwords or tenants is dangerous. When Python is executed with optimization flags (`python -O`), assertions can be optimized away, or catching `AssertionError` can mask real bugs.
  - **Race Conditions on Membership:** In `Identity.add_user_to_tenant`, reading `user_ref['tenants']`, mutating the set, and updating the dict is not atomic. Concurrent requests will cause lost updates.

#### Interview Questions & Discussion Angles
1. **Python Exception Semantics:**
   > *"Why should `AssertionError` never be used for business logic validation or control flow (such as rejecting invalid passwords) in production Python applications?"*
2. **In-Memory Store Concurrency & Shallow Copies:**
   > *"Review `DictKvs.set`. What happens if a stored dictionary contains nested dictionaries or lists? What are the implications of shallow copying (`value.copy()`) versus deep copying in a shared data store?"*
3. **Key-Value Namespace Collisions:**
   > *"When multiple independent subsystems share a single global flat key-value store, how should key namespacing and secondary indexing be structured to prevent collisions and orphan records?"*

---

### Decision 5: Non-Invasive Extension Routing & Diablo Backward Compatibility Hacks

- **Relevant Files:**
  - `keystone/common/wsgi.py` (`Router`, `ExtensionRouter`, `ComposingRouter`)
  - `keystone/contrib/admin_crud/core.py` (`CrudExtension`)
  - `keystone/identity/core.py` (`RoleController.get_role_refs`, `RoleController.delete_role_ref`)

#### The Code Decision
Keystone separates core token/auth endpoints from administrative CRUD operations, treating CRUD as an optional extension (`CrudExtension`, historically known as `OS-KSADM`). To attach extension routes without modifying core code, Keystone introduces `ExtensionRouter`:

```python
class ExtensionRouter(Router):
    def __init__(self, application, mapper=None):
        if mapper is None:
            mapper = routes.Mapper()
        self.application = application
        self.add_routes(mapper)
        mapper.connect('{path_info:.*}', controller=self.application)
        super(ExtensionRouter, self).__init__(mapper)
```

Furthermore, in `keystone/identity/core.py`, Keystone had to maintain compatibility with Diablo clients that expected `roleRefs` to be first-class entities with string IDs, even though Keystone Light eliminated the `role_ref` table. The authors solved this with a composite key serialization hack:

```python
# COMPAT(diablo): CRUD extension
def get_role_refs(self, context, user_id):
    """Ultimate hack to get around having to make role_refs first-class."""
    o = []
    for tenant_id in tenant_ids:
        for role_id in role_ids:
            ref = {'roleId': role_id, 'tenantId': tenant_id, 'userId': user_id}
            ref['id'] = urllib.urlencode(ref)
            o.append(ref)
    return {'roles': o}

def delete_role_ref(self, context, user_id, role_ref_id):
    role_ref_ref = urlparse.parse_qs(role_ref_id)
    tenant_id = role_ref_ref.get('tenantId')[0]
    role_id = role_ref_ref.get('roleId')[0]
    self.identity_api.remove_role_from_user_and_tenant(context, user_id, tenant_id, role_id)
```

#### Context & Tradeoffs
- **Advantages:**
  - `ExtensionRouter` wraps the core WSGI application, handling extension routes first and forwarding unmatched traffic to `self.application` via `{path_info:.*}`.
  - The URL-encoded composite ID hack satisfied legacy client contracts without requiring an additional relational table or database migration.
- **Vulnerabilities & Edge Cases:**
  - **Brittle ID Parsing:** Parsing synthetic query strings out of an entity ID parameter assumes clients will preserve and return the string without modification or URL decoding.
  - **Dual Namespace Routing:** In `keystone/contrib/admin_crud/core.py`, routes are duplicated (e.g. `/users/{id}/password` and `/users/{id}/OS-KSADM/password`), increasing routing table complexity and potential drift between legacy and modern endpoints.

#### Interview Questions & Discussion Angles
1. **API Evolution & Synthetic Identifiers:**
   > *"Review the `get_role_refs` and `delete_role_ref` implementation in `keystone/identity/core.py`. What are the risks and trade-offs of encoding relational tuples into synthetic string IDs to satisfy legacy client contracts without schema changes?"*
2. **WSGI Pipeline & Route Fallback:**
   > *"How does `ExtensionRouter` use Routes middleware and catch-all path matching (`{path_info:.*}`) to allow pluggable extensions to intercept requests ahead of core application routers?"*
3. **REST Semantics & Idempotency:**
   > *"In `CrudExtension`, update operations use HTTP `PUT` but perform partial dictionary updates. What is the semantic difference between `PUT` and `PATCH`, and what problems arise when `PUT` is treated as a partial update?"*

---

### Decision 6: AWS EC2/S3 Interoperability & Signature Verification Pitfalls

- **Relevant Files:**
  - `keystone/contrib/ec2/core.py` (`Ec2Controller.check_signature`, `authenticate`)
  - `keystone/common/utils.py` (`Ec2Signer`)
  - `keystone/contrib/s3/core.py` (`S3Controller.check_signature`)

#### The Code Decision
To enable OpenStack services (like Nova and Swift) to accept requests signed by AWS SDKs (boto, AWS CLI), Keystone implements AWS signature verification. In `keystone/contrib/ec2/core.py`, `Ec2Controller` validates HMAC-SHA1 and HMAC-SHA256 signatures, minting an OpenStack token upon success:

```python
def check_signature(self, creds_ref, credentials):
    signer = utils.Ec2Signer(creds_ref['secret'])
    signature = signer.generate(credentials)
    if signature == credentials['signature']:
        return
    # NOTE(vish): Some libraries don't use the port when signing requests, so try again without port.
    elif ':' in credentials['signature']:
        hostname, _port = credentials['host'].split(":")
        credentials['host'] = hostname
        signature = signer.generate(credentials)
        if signature != credentials.signature:
            msg = "Invalid signature"
            raise webob.exc.HTTPUnauthorized(explanation=msg)
    else:
        msg = "Signature not supplied"
        raise webob.exc.HTTPUnauthorized(explanation=msg)
```

In `keystone/common/utils.py`, `Ec2Signer` re-implements AWS signature algorithms v0, v1, and v2, sorting query parameters alphabetically and generating base64-encoded HMAC hashes.

#### Context & Tradeoffs
- **Critical Bugs in Retry Logic:**
  1. `elif ':' in credentials['signature']:` checks for a colon in the *signature* rather than in `credentials['host']`! If a host has a port (`localhost:8773`), the colon check on the base64 signature evaluates to `False`, skipping the port-stripping retry altogether.
  2. `if signature != credentials.signature:` attempts attribute access on `credentials`, which is a plain dictionary. If reached, it raises `AttributeError: 'dict' object has no attribute 'signature'`, causing a 500 Internal Server Error instead of a 401 Unauthorized.
- **Architectural Tradeoffs:**
  - **Bridging Flat Accounts to Multi-Tenancy:** AWS credentials possess no concept of OpenStack tenants. Keystone bridges this by binding each EC2 credential pair to both a `user_id` and a specific `tenant_id`.
  - **Shared Secret Exposure:** Verifying HMAC signatures requires the identity service to store the user's secret key in reversible plaintext (or reversible encryption), rather than an irreversible one-way hash (like bcrypt).

#### Interview Questions & Discussion Angles
1. **Cryptographic Signatures & Secret Storage:**
   > *"Why can passwords be stored as one-way salted hashes (bcrypt/argon2), while AWS-style HMAC request signatures require the server to access the plaintext secret key? How should secret keys be protected at rest?"*
2. **Code Review & Error Path Verification:**
   > *"Analyze lines 90-108 in `keystone/contrib/ec2/core.py`. Identify the two critical bugs in the fallback signature verification logic."*
3. **Timing Attacks on Cryptographic Signatures:**
   > *"Why is comparing cryptographic signatures using standard Python string equality (`signature == credentials['signature']`) vulnerable to timing attacks, and how does constant-time comparison (`hmac.compare_digest`) mitigate this?"*

---

### Decision 7: Redundant Dual-Roundtrip Auth Token Middleware & Delegated Auth

- **Relevant Files:**
  - `keystone/middleware/auth_token.py` (`AuthProtocol.__call__`, `_validate_claims`, `_expound_claims`)
  - `keystone/middleware/core.py` (`Debug.print_generator`, `JsonBodyMiddleware`)

#### The Code Decision
To authenticate incoming client requests, downstream OpenStack services (Nova compute, Glance image service) embed Keystone's `AuthProtocol` middleware in their WSGI pipelines.

In `keystone/middleware/auth_token.py`, when a request presents a token, the middleware validates it and extracts user claims:
```python
def __call__(self, env, start_response):
    claims = self._get_claims(env)
    valid = self._validate_claims(claims)
    if valid:
        claims = self._expound_claims(claims)
```
Examining `_validate_claims` and `_expound_claims` reveals that **both methods execute an identical HTTP request to the exact same Keystone endpoint (`GET /v2.0/tokens/<token>`)**:
```python
# In _validate_claims:
conn = http_connect(self.auth_host, self.auth_port, 'GET',
                    '/v2.0/tokens/%s' % claims, headers=headers)
resp = conn.getresponse()
if not str(resp.status).startswith('20'):
    return False
return True

# In _expound_claims:
conn = http_connect(self.auth_host, self.auth_port, 'GET',
                    '/v2.0/tokens/%s' % claims, headers=headers)
resp = conn.getresponse()
data = resp.read()
# Unpacks data...
```
A code comment explicitly acknowledges this:
`#TODO(Ziad): there is an optimization we can do here. We have just received data from Keystone that we can use instead of making another call in _expound_claims`.

Furthermore, `keystone/middleware/core.py:Debug` attempts to print request and response streams using `sys.stdout.write(part)`, but **omits `import sys`**, creating a runtime `NameError` whenever the debug middleware is invoked.

#### Context & Tradeoffs
- **Advantages:**
  - Decouples downstream services from Keystone database credentials; services only need an HTTP client and an administrative token to validate user requests.
  - `delay_auth_decision` mode permits unauthenticated requests to flow downstream with `X_IDENTITY_STATUS = 'Invalid'`, allowing services to serve public or anonymous resources.
- **Vulnerabilities & Edge Cases:**
  - **Severe Network Latency Amplification:** Making two synchronous HTTP roundtrips to Keystone for *every single incoming request* to Nova or Swift creates an extreme performance bottleneck.
  - **Lack of Local Caching:** Without a local token cache (e.g. Memcached), a flurry of requests using the same token hammers Keystone repeatedly.
  - **Header Spoofing Risks:** If external requests reach downstream services without passing through `AuthProtocol`, an attacker could forge `X-User-Id` and `X-Roles: admin` headers.

#### Interview Questions & Discussion Angles
1. **Distributed System Performance Bottlenecks:**
   > *"In `keystone/middleware/auth_token.py`, every incoming request incurs two back-to-back synchronous HTTP calls to validate the token and fetch claims. How would you design a distributed caching layer to optimize this, and how would you handle instantaneous token revocation?"*
2. **Security & Header Injection:**
   > *"When microservices trust identity headers (`X-User-Id`, `X-Roles`) passed from upstream WSGI middleware, how do you prevent clients from spoofing these headers at the load balancer or API gateway?"*
3. **Delegated Authorization & Anonymous Access:**
   > *"What are the architectural benefits and security pitfalls of `delay_auth_decision` mode in authentication middleware?"*

---

### Decision 8: Reflection-Based CLI Generation & Hardcoded Driver Migrations

- **Relevant Files:**
  - `keystone/cli.py` (`BaseApp`, `DbSync`, `ClientCommand`)
  - `keystone/common/cfg.py` (`ConfigOpts`, `register_opt`)
  - `keystone/config.py` (`ConfigMixin`)

#### The Code Decision
In `keystone/cli.py`, instead of manually authoring CLI commands for every entity type, Keystone implements `ClientCommand`, which uses Python runtime reflection (`dir()`) to inspect client handles in `python-keystoneclient` and automatically map client SDK methods to CLI subcommands:

```python
class ClientCommand(BaseApp):
    def __init__(self, *args, **kw):
        super(ClientCommand, self).__init__(*args, **kw)
        self.client = kc.Client(CONF.endpoint, token=CONF.auth_token)
        self.handle = getattr(self.client, self._attr_name())
        self._build_action_map()

    def _build_action_map(self):
        actions = {}
        for k in dir(self.handle):
            if not k.startswith('_'):
                actions[k] = k
        self.ACTION_MAP.update(actions)
```

Meanwhile, in `DbSync`:
```python
class DbSync(BaseApp):
    name = 'db_sync'
    def main(self):
        for k in ['identity', 'catalog', 'policy', 'token']:
            driver = utils.import_object(getattr(CONF, k).driver)
            if hasattr(driver, 'db_sync'):
                driver.db_sync()
```

#### Context & Tradeoffs
- **Advantages:**
  - Adding a method to the client SDK automatically exposes it in the CLI without touching `keystone/cli.py`.
  - Supports flexible command-line arguments using dynamic key-value parsing (`key=value` and `key[]=value`).
- **Vulnerabilities & Edge Cases:**
  - **Unchecked Parameter Injection:** Because arguments are parsed dynamically and passed to methods using reflection, CLI validation is non-existent until the SDK method raises an exception.
  - **Omission of Extension Tables:** `DbSync.main()` hardcodes the list of core subsystems (`['identity', 'catalog', 'policy', 'token']`). Pluggable extensions like `keystone.contrib.ec2` are completely ignored by `keystone-manage db_sync`, requiring manual table creation or external scripts!

#### Interview Questions & Discussion Angles
1. **Reflection in Tooling Design:**
   > *"What are the pros and cons of using runtime object reflection (`dir(sdk_client)`) to dynamically generate command-line interfaces compared to explicit declarative CLI frameworks (like Click, Cobra, or Argparse)?"*
2. **Extensibility in Database Migrations:**
   > *"Why is hardcoding core driver names in a database migration runner an architectural anti-pattern for extensible plugin architectures, and how should an extension migration registry be designed?"*

---

## 3. Per-File Summary & Codebase Index

---

### Root Directory
- `setup.py`: Setuptools installation script. Dynamically reads `tools/pip-requires` and registers console script entry points (`keystone-all`, `keystone-manage`).
  - *Interview angle:* Risks of dynamic requirement file parsing inside packaging scripts.
- `run_tests.py`: Python test runner wrapper around `nose.core.run`, injecting coverage and test options.
  - *Interview angle:* Test runner customization vs. standard test harnesses.
- `run_tests.sh`: Bash wrapper managing virtualenv detection and test execution flags.
- `tox.ini`: Tox configuration for multi-version testing (Python 2.6, 2.7, PEP8).
- `README.rst`: Architectural vision, explaining the "shim" design pattern and optional CRUD approach.
- `HACKING.rst`: Style commandments enforcing import ordering, TODO conventions, and discouraging bare `except:`.
- `TODO`: Prototype task list noting missing validation and policy tests.
- `.gitignore`: Specifies untracked files including SQLite databases (`bla.db`) and virtual environments.

### `bin/` Directory
- `bin/keystone-all`: Daemon launcher hosting public (5000) and admin (35357) Eventlet servers in a single process. Contains the `keystone-all/__init__.py` path resolution bug.
  - *Interview angle:* Single-process multi-port architectures, greenlet concurrency, and path resolution bugs.
- `bin/keystone-manage`: Administrative CLI tool delegating commands directly to `keystone.cli.main()`.

### `etc/` Directory
- `etc/keystone.conf`: Master configuration file. Defines database URLs, backend drivers, and Paste Deploy WSGI pipelines.
  - *Interview angle:* Static administrative token security risks; pipeline filter ordering.
- `etc/default_catalog.templates`: Catalog endpoint definitions using `$(...)s` token replacement.
- `etc/logging.conf.sample`: Standard Python logging configuration for console and file output.

### `keystone/` Top-Level Modules
- `keystone/__init__.py`: Package marker keeping top-level namespace clean.
- `keystone/cli.py`: Administrative CLI framework. Uses SDK reflection to dynamically map commands; implements dynamic `key=val` parsing; contains hardcoded driver list in `DbSync`.
  - *Interview angle:* Reflection-based CLI dispatch; missing extension database synchronization.
- `keystone/config.py`: Configuration wrapper defining option registration helpers and the global `CONF` singleton.
  - *Interview angle:* Global configuration singletons vs. explicit dependency injection.
- `keystone/service.py`: Core WSGI routers and controllers (`TokenController`, `VersionController`). Manages token issuance, unscoped-to-scoped token re-scoping, and catalog transformation. Contains the `raise NotImplemented()` language quirk.
  - *Interview angle:* Token scoping models; in-place catalog dictionary mutation; `NotImplemented` vs `NotImplementedError`.
- `keystone/test.py`: Base test harness. Implements `TestClient` for in-memory WSGI testing, dynamic ephemeral port binding (`serveapp`), and order-independent deep equality assertions (`assertDeepEquals`).
  - *Interview angle:* In-memory WSGI simulation vs. live HTTP socket testing; structural JSON equality algorithms.

### `keystone/catalog/` Subsystem
- `keystone/catalog/__init__.py`: Package marker re-exporting `Manager` and `ServiceController`.
- `keystone/catalog/core.py`: Catalog manager and `ServiceController` application for administering catalog services under the `OS-KSADM` namespace.
- `keystone/catalog/backends/__init__.py`: Package marker for catalog storage drivers.
- `keystone/catalog/backends/kvs.py`: In-memory KVS catalog driver with manual `service_list` indexing.
- `keystone/catalog/backends/templated.py`: Flat-file catalog template parser performing runtime string interpolation. Contains unclosed file descriptor calls.
  - *Interview angle:* String interpolation security risks; file descriptor leaks.

### `keystone/common/` Subsystem
- `keystone/common/__init__.py`: Package marker for shared utilities.
- `keystone/common/bufferedhttp.py`: Monkey-patches `httplib.HTTPResponse` with buffered socket reading for small HTTP requests.
  - *Interview angle:* Network socket buffering; hazards of monkey-patching standard libraries.
- `keystone/common/cfg.py`: Precursor to `oslo.config`. Implements typed command-line and INI configuration parsing.
  - *Interview angle:* Custom configuration schemas vs. standard library parsers.
- `keystone/common/kvs.py`: In-memory key-value primitives (`DictKvs`, `Base`). Contains the global `INMEMDB` singleton and the `value[:]` non-sliceable type error.
  - *Interview angle:* Shared in-memory state; shallow copying flaws; type handling bugs.
- `keystone/common/logging.py`: Centralized logging format configuration.
- `keystone/common/manager.py`: Manager layer dynamically proxying calls to backend drivers while stripping the `context` parameter and caching wrappers via `setattr`.
  - *Interview angle:* Context stripping trade-offs; dynamic attribute delegation.
- `keystone/common/utils.py`: Utilities including `Ec2Signer` (AWS v0/v1/v2 HMAC signature implementation), bcrypt password hashing, and custom JSON serialization (`SmarterEncoder`).
  - *Interview angle:* Password hashing work factors; constant-time string comparisons.
- `keystone/common/wsgi.py`: Micro-framework atop WebOb and Routes. Implements `Application` dynamic action dispatch, `Middleware`, `Debug`, and `ExtensionRouter` fallback routing.
  - *Interview angle:* WSGI protocol fundamentals; non-invasive extension routing.

### `keystone/common/sql/` Subsystem
- `keystone/common/sql/__init__.py`: Package marker re-exporting SQLAlchemy model symbols.
- `keystone/common/sql/core.py`: SQLAlchemy session and engine management, `NullPool` for SQLite, `JsonBlob` schemaless type decorator, and `DictBase` model mixin with stateful `__iter__` bug.
  - *Interview angle:* Schemaless JSON columns in RDBMS; iterator state mutation bugs; SQLite connection pooling.
- `keystone/common/sql/migration.py`: Migration wrapper around `migrate.versioning.api`.
- `keystone/common/sql/util.py`: Test database setup helper hardcoding unlinking `bla.db`.
- `keystone/common/sql/migrate_repo/manage.py`: Generated migration management script.
- `keystone/common/sql/migrate_repo/migrate.cfg`: Migration repository configuration.
- `keystone/common/sql/migrate_repo/README`: Documentation placeholder.
- `keystone/common/sql/migrate_repo/versions/__init__.py`: Package marker for versions.
- `keystone/common/sql/migrate_repo/versions/001_add_initial_tables.py`: Initial schema migration using `create_all()`. Downgrade is an empty `pass`.

### `keystone/contrib/` Subsystem
- `keystone/contrib/__init__.py`: Package marker for extensions.
- `keystone/contrib/admin_crud/__init__.py`: Package marker exposing `CrudExtension`.
- `keystone/contrib/admin_crud/core.py`: OS-KSADM CRUD extension wiring management routes onto identity and catalog controllers. Connects dual routes with and without `/OS-KSADM/` prefixes.
  - *Interview angle:* Treating administrative CRUD as an optional extension; PUT vs. PATCH partial updates.
- `keystone/contrib/ec2/__init__.py`: Package marker exposing `Ec2Extension`.
- `keystone/contrib/ec2/core.py`: AWS EC2 credential controller and signature validator. Bridges flat AWS credentials to multi-tenant accounts. Contains port-stripping dictionary lookup bugs.
  - *Interview angle:* AWS signature verification; error-handling bugs in retry paths.
- `keystone/contrib/ec2/backends/__init__.py`: Package marker for EC2 storage.
- `keystone/contrib/ec2/backends/kvs.py`: In-memory KVS driver for EC2 credentials.
- `keystone/contrib/ec2/backends/sql.py`: SQLAlchemy ORM driver for EC2 credentials.
- `keystone/contrib/s3/__init__.py`: Package marker exposing `S3Extension`.
- `keystone/contrib/s3/core.py`: AWS S3 authentication extension subclassing `Ec2Controller` and overriding `check_signature`. Raises generic Python `Exception`.

### `keystone/identity/` Subsystem
- `keystone/identity/__init__.py`: Package marker exposing identity interfaces and routers.
- `keystone/identity/core.py`: Core identity logic. Implements `TenantController`, `UserController`, `RoleController`, and the URL-encoded composite ID hack for Diablo `roleRefs` compatibility.
  - *Interview angle:* Synthetic serialized composite IDs for backward compatibility; rejecting user-only roles.
- `keystone/identity/backends/__init__.py`: Package marker for identity drivers.
- `keystone/identity/backends/kvs.py`: In-memory identity driver with manual secondary indices. Raises `AssertionError` for invalid credentials; contains race conditions in membership updates.
  - *Interview angle:* Control flow via assertions; read-modify-write race hazards.
- `keystone/identity/backends/pam.py`: Linux PAM identity driver. Contains the silent equality assignment bug (`metadata['is_admin'] == True`) and reversed return tuple ordering.
  - *Interview angle:* Host PAM integration risks; critical code-review bug detection.
- `keystone/identity/backends/sql.py`: Relational identity driver storing extra attributes in `JsonBlob`. Mutates input dictionaries directly in `_filter_user`.

### `keystone/middleware/` Subsystem
- `keystone/middleware/__init__.py`: Package marker for middlewares.
- `keystone/middleware/auth_token.py`: Downstream authentication middleware. Executes redundant back-to-back HTTP calls to Keystone to validate tokens and fetch claims; supports `delay_auth_decision`.
  - *Interview angle:* Latency amplification from redundant HTTP calls; distributed token caching and revocation.
- `keystone/middleware/core.py`: Internal WSGI filters (`TokenAuthMiddleware`, `AdminTokenAuthMiddleware`, `JsonBodyMiddleware`). `Debug` middleware contains missing `import sys`.
  - *Interview angle:* Parameter sanitization against injection; missing imports in debug code.
- `keystone/middleware/ec2_token.py`: Nova middleware shim intercepting EC2 requests. Directly imports `nova` internals.
- `keystone/middleware/glance_auth_token.py`: Glance middleware shim building context objects from identity headers. Directly imports `glance`.
- `keystone/middleware/nova_auth_token.py`: Alias re-exporting `auth_token.py`.
- `keystone/middleware/nova_keystone_context.py`: Nova middleware shim converting HTTP headers into `RequestContext`.
  - *Interview angle:* Header spoofing vulnerabilities in microservices.
- `keystone/middleware/swift_auth.py`: Swift middleware shim checking container ACLs and reseller admin status. Directly imports `swift`.

### `keystone/policy/` Subsystem
- `keystone/policy/__init__.py`: Package marker for policy subsystem.
- `keystone/policy/core.py`: Policy manager loading configured policy driver.
- `keystone/policy/backends/__init__.py`: Package marker for policy backends.
- `keystone/policy/backends/simple.py`: Policy rule evaluator (`SimpleMatch`). Uses `can_haz` method name; evaluates only OR logic; returns implicit `None` on failure; contains type comparison mismatches.
  - *Interview angle:* Implicit `None` vs explicit `False`; rule evaluation engines.

### `keystone/token/` Subsystem
- `keystone/token/__init__.py`: Package marker exposing token interface.
- `keystone/token/core.py`: Core token driver contract definition.
- `keystone/token/backends/__init__.py`: Package marker for token backends.
- `keystone/token/backends/kvs.py`: In-memory KVS token store. Deliberate omission of an SQL backend for ephemeral tokens.
  - *Interview angle:* Suitability of KVS vs. RDBMS for high-churn token workloads.

### `tests/` Directory
- `tests/backend_sql.conf`: Test configuration targeting SQLite.
- `tests/default_catalog.templates`: Catalog template test fixture.
- `tests/default_fixtures.py`: Centralized test data fixtures (users, tenants, roles).
- `tests/test_backend.py`: Defines `IdentityTests` mixin class for multi-backend contract testing.
  - *Interview angle:* Test mixins for backend parity enforcement.
- `tests/test_backend_kvs.py`: Runs backend tests against in-memory KVS.
- `tests/test_backend_sql.py`: Runs backend tests against SQLite database.
- `tests/test_cli.py`: Unit tests for CLI parameter parsing.
- `tests/test_keystoneclient.py`: Integration test suite spinning up live servers and testing against `python-keystoneclient`.
- `tests/test_keystoneclient_sql.py`: Integration tests for client SDK against SQL backend.
- `tests/test_legacy_compat.py`: Regression test verifying payload compatibility against Diablo documentation samples.
- `tests/test_novaclient_compat.py`: Compatibility tests using `novaclient`.
- `tests/test_overrides.conf`: Test configuration overrides.

### `tools/` Directory
- `tools/install_venv.py`: Bootstraps isolated virtual environments and installs pip requirements.
- `tools/pip-requires`: Runtime dependency list (Eventlet, WebOb, Routes, PasteDeploy, SQLAlchemy, bcrypt).
- `tools/pip-requires-test`: Test dependency list (Nose, Coverage, Flake8).
- `tools/with_venv.sh`: Shell wrapper executing commands inside virtual environments.

### `docs/` Directory
- `docs/Makefile`: Sphinx build Makefile.
- `docs/keystone_compat_flows.sdx`: Sequence diagram source file for authentication flows.
- `docs/source/conf.py`: Sphinx documentation configuration.
- `docs/source/index.rst`: Main documentation index.
- `docs/source/architecture.rst`: Architectural documentation.
- `docs/source/middleware_architecture.rst`: Sequence flows and middleware specifications.
- `docs/source/man/keystone-all.rst`: Manual page for `keystone-all`.
- `docs/source/man/keystone-manage.rst`: Manual page for `keystone-manage`.

---

## 4. Interview Question Quick-Reference Matrix

Use this matrix to match interview focus areas with specific Keystone Light mechanisms, target files, and challenge scenarios:

| Focus Area | Keystone Mechanism | Target Files | Key Challenge / Discussion Prompt |
| :--- | :--- | :--- | :--- |
| **Object Delegation & Metaprogramming** | Dynamic manager proxy & context stripping | `keystone/common/manager.py` | Architectural trade-offs of dynamic `__getattr__` and wrapper caching via `setattr` vs. explicit ABC interfaces; consequences of stripping request context. |
| **Concurrency & Systems** | Dual-port daemon & Eventlet coroutines | `bin/keystone-all`, `keystone/common/wsgi.py` | Shared failure domains of co-locating admin and public endpoints in one process; risks of CPU-bound operations (bcrypt) starving the coroutine event loop. |
| **Database Modeling & ORM** | Hybrid relational/document model & `DictBase` | `keystone/common/sql/core.py`, `keystone/identity/backends/sql.py` | Storing unindexed dynamic attributes in JSON columns vs. EAV tables; diagnosing stateful `__iter__` mutation bugs on shared model instances. |
| **In-Memory Storage & Quirks** | Global singleton KVS & shallow copying | `keystone/common/kvs.py` | Thread-safety of shared global dictionaries; shallow copy vs. deep copy hazards; debugging slice-assignment `TypeError` bugs on non-sliceable values. |
| **API Evolution & REST Design** | Non-invasive extension routing & Diablo hacks | `keystone/common/wsgi.py`, `keystone/identity/core.py` | Wrapping WSGI routing trees with catch-all `{path_info:.*}` fallbacks; encoding relational tuples into synthetic query-string IDs to avoid schema migrations. |
| **Cryptography & Interoperability** | AWS EC2/S3 HMAC signatures & bug hunting | `keystone/contrib/ec2/core.py`, `keystone/common/utils.py` | Reversible secret key storage vs. one-way password hashing; timing attack vulnerabilities of `==`; spotting dictionary attribute-lookup bugs in retry paths. |
| **Distributed Middleware & Performance**| Redundant HTTP roundtrips & delegated auth | `keystone/middleware/auth_token.py` | Eliminating duplicate synchronous HTTP roundtrips in critical request paths; distributed caching vs. token revocation; header spoofing prevention. |
| **Language Bugs & Edge Cases** | Silent equality & missing imports | `keystone/identity/backends/pam.py`, `keystone/middleware/core.py`, `keystone/service.py` | Spotting the `==` vs `=` assignment bug in PAM auth; missing `import sys` in debug middleware; raising `NotImplemented` constant vs `NotImplementedError`. |
| **Testing Architecture** | Multi-backend test mixins & live port binding | `tests/test_backend.py`, `keystone/test.py` | Enforcing cross-backend parity using test mixin inheritance; in-memory WSGI simulation vs. live socket testing with ephemeral port discovery. |
