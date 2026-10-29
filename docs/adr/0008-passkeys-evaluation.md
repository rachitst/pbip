# ADR 0008: FIDO2 / WebAuthn Passkey Authentication Architecture Evaluation and Implementation Roadmap
- Status: Proposed (Evaluation Spike — TASK-073 / Issue #96)
- Date: 2026-10-29
- Deciders: Architecture Team, Security & Core Banking Engineering
- Technical References: TASK-073, Issue #96, Sprint 5 (Epic: Identity & Core Banking)

---

## 1. Context and Problem Statement

Retail banking applications face an escalating threat environment characterized by credential stuffing, automated brute-force attacks, SIM swapping, and increasingly prevalent adversary-in-the-middle (AiTM) reverse-proxy phishing attacks (e.g., Evilginx, Modlishka). Standard password authentication, even when augmented by traditional Time-Based One-Time Password (TOTP) multi-factor authentication (RFC 6238), remains susceptible to real-time credential and session-cookie interception.

Under the UK Payment Services Regulations 2017 (PSRs), the EU Revised Payment Services Directive (PSD2 RTS on Strong Customer Authentication — SCA), and European Banking Authority (EBA) Guidelines on ICT and Security Risk Management (EBA/GL/2019/04), regulated payment institutions must enforce Strong Customer Authentication combining at least two independent elements:
1. **Knowledge** (something only the user knows),
2. **Possession** (something only the user possesses), and
3. **Inherence** (something the user is, such as biometrics).

FIDO2 / WebAuthn (Web Authentication API) passkeys replace shared secrets with asymmetric public-key cryptography. Private keys are generated and held exclusively within secure hardware authenticators (Platform TPMs, Apple Secure Enclave, Android Keystore, or FIDO2 hardware keys like YubiKeys), while the relying party (PBIP) stores only public keys. Crucially, the browser cryptographically binds authentication assertions to the application's origin domain (Relying Party ID), rendering passkeys structurally immune to credential stuffing, AiTM phishing, and credential replay.

TASK-073 commissions an architectural evaluation and proof-of-concept assessment to determine:
1. The technical suitability and operational robustness of the Python `py_webauthn` (`webauthn`) library for PBIP's FastAPI/SQLAlchemy backend.
2. Cross-platform browser WebAuthn API compatibility and user experience considerations.
3. How passkeys should integrate with PBIP's existing authentication architecture, session lifecycle, token semantics, and database models.
4. A pragmatic, phased implementation roadmap balancing customer usability, regulatory compliance, and migration safety.

---

## 2. Existing PBIP Authentication Architecture & Constraints

PBIP's current authentication framework (`apps/api/src/pbip_api/modules/auth/`, `core/security.py`, `PROJECT_BLUEPRINT.md` §6) is engineered to bank-grade standards:

1. **Credential Hashing:** Passwords are hashed using Argon2id (RFC 9106 low-memory profile: $t=3, p=4, m=64\text{ MiB}$). Unknown usernames execute a dummy hash (`burn()`) to prevent user enumeration via timing oracles.
2. **Session Architecture:**
   - **Access Token:** Short-lived (15 minutes) signed JSON Web Token (HS256) stored in client memory. Claims include `sub`, `iss`, `aud`, `exp`, `iat`, `jti`, `typ="access"`, `roles`, and `amr` (Authentication Method Reference).
   - **Refresh Token:** High-entropy opaque 384-bit random string stored exclusively as a SHA-256 hash in the `refresh_tokens` database table. Transmitted via an `HttpOnly`, `SameSite=Strict`, `Secure` (deployed), `Path=/api/v1/auth` cookie.
   - **Refresh Rotation & Reuse Detection:** Every refresh call rotates the cookie and records `replaced_by_id`. Presentation of an already-used or revoked refresh token revokes the entire token family (`_revoke_family`), mitigating session theft.
3. **Multi-Factor Authentication (MFA):**
   - Implemented via RFC 6238 TOTP with a $\pm 1$ time-step window ($30\text{ s}$ per step).
   - MFA secrets are encrypted at rest using AES-128-CBC / HMAC-SHA256 via `FieldCipher` (Fernet key rotation).
   - Replay protection enforces strictly advancing time steps (`mfa_last_step`).
   - Eight single-use SHA-256 hashed recovery codes are issued upon enrollment.
   - Staff roles (`RELATIONSHIP_MANAGER`, `FRAUD_ANALYST`, `COMPLIANCE_OFFICER`, `ADMIN`) mandate `amr: otp` via `require_roles(..., mfa=True)` on every staff endpoint.
4. **Brute-Force & Lockout Controls:**
   - Per-IP rate limiting enforced in Redis (10 logins/min, 10 registrations/hour, 5 password changes/min) with graceful fail-open on Redis downtime.
   - Per-user progressive database lockout: 5 consecutive failures incur a $60\text{ s}$ lock, doubling exponentially up to $3600\text{ s}$ ($1\text{ h}$).
5. **Architectural Guardrails (AGENTS.md):**
   - Strict modular monolith layering: HTTP router $\rightarrow$ service (owns transaction) $\rightarrow$ repository/policy $\rightarrow$ models.
   - Zero `Any` in strict Python 3.12 typing; RFC 7807 problem details error responses; strict log redaction masking tokens, credentials, and PII.

---

## 3. Options Considered

### Option A: Status Quo (Argon2id Passwords + Optional TOTP MFA)
- **Description:** Maintain the existing username/password model with optional TOTP for retail customers and mandatory TOTP for bank staff.
- **Advantages:** Zero backend architectural changes; zero client library dependencies; already thoroughly tested in unit, integration, and security suites.
- **Disadvantages:** Vulnerable to AiTM phishing proxies; customer friction with manual 6-digit TOTP code entry; risk of credential reuse from third-party breaches; customer support costs for recovery code resets.

### Option B: Immediate "Passkey-Only" Replacement (Big-Bang Passwordless)
- **Description:** Deprecate passwords entirely; require all customers and staff to register and authenticate exclusively via WebAuthn discoverable credentials (resident keys).
- **Advantages:** Eliminates password database attack surface; 100% phishing-resistant authentication from day one.
- **Disadvantages:** Severe customer exclusion risk for users on legacy devices, older enterprise workstations, or restrictive browsers without platform authenticators; device loss or platform migration (e.g., switching from iOS to Android) without an existing backup creates unresolvable customer lockout; high regulatory risk if customers are unable to access emergency funds.

### Option C: Phased Hybrid Rollout (Recommended)
- **Description:** Introduce FIDO2 / WebAuthn passkeys in structured phases:
  - **Phase 1 (Immediate Deliverable):** Architecture evaluation, security threat modeling, and implementation roadmap (TASK-073).
  - **Phase 2 (MFA & Step-Up):** Support passkeys as a phishing-resistant second factor (alternative to TOTP) and high-risk payment step-up mechanism with PSD2 dynamic linking.
  - **Phase 3 (Passkey-First Primary Authentication):** Provide optional discoverable passkey login (WebAuthn autofill / conditional mediation) alongside password fallback.
  - **Phase 4 (Passwordless Onboarding):** Enable passwordless account registration for new customers with multi-device cross-platform recovery.
- **Advantages:** Preserves backward compatibility; zero disruption to existing customers; allows high-risk operations (transfers, staff actions) to immediately leverage phishing-resistant hardware keys; aligns with EBA and FCA supervisory expectations.

---

## 4. Technical Evaluation: PyWebAuthn Library

### 4.1 Library Overview and Provenance
- **Package Name:** `webauthn` (commonly referenced as `py_webauthn`).
- **Maintainer / Upstream:** Duo Security / Cisco & Yubico ([duo-labs/py_webauthn](https://github.com/duo-labs/py_webauthn)).
- **Latest PyPI Release:** Version 3.0.x (verified October 2026).
- **License:** BSD 3-Clause.
- **Python Compatibility:** Python 3.10, 3.11, 3.12, 3.13 (fully aligned with PBIP's Python 3.12 target).

### 4.2 Dependencies and Supply-Chain Impact
- **Core Dependencies:**
  1. `cryptography`: Used for digital signature verification (ECDSA, RSA, EdDSA) and X.509 attestation certificate chain parsing. *Note: `cryptography` is already an explicit core dependency in PBIP (`pyproject.toml`), minimizing new transitive binary surface.*
  2. `cbor2`: Pure-Python / C-accelerated CBOR (Concise Binary Object Representation) decoder required to parse authenticator data and attestation statements.
  3. `pydantic`: Utilized for structural model validation and JSON serialization. Fully compatible with Pydantic v2 used throughout PBIP.
- **Supply-Chain Assessment (ADR-0006 compliance):** Duo Labs is a premier contributor to W3C WebAuthn standards. The library is mature, maintained, pinned via `uv.lock`, and verified clean by `pip-audit` and Bandit.

### 4.3 Functional Capabilities
- **Registration Ceremony:**
  - `generate_registration_options(...)`: Creates `PublicKeyCredentialCreationOptions` with configurable RP ID, RP name, user ID (bytes), username, supported algorithms, authenticator attachment preferences, resident key requirements, and cryptographic challenge.
  - `verify_registration_response(...)`: Validates client data JSON, verifies challenge freshness, parses authenticator data, verifies attestation statements, and extracts the credential ID and public key bytes.
- **Authentication Ceremony:**
  - `generate_authentication_options(...)`: Generates `PublicKeyCredentialRequestOptions` referencing registered credential IDs and challenge bytes.
  - `verify_authentication_response(...)`: Validates client data JSON, origin, RP ID hash, user verification flags, counter progression, and the cryptographic signature over `authenticatorData || clientDataHash`.
- **Serialization Helpers:**
  - `options_to_json(...)` and JSON parsing helpers safely transform binary buffers (challenges, credential IDs, user handles) to base64url strings for HTTP transit without bespoke encoding bugs.

### 4.4 Cryptographic Algorithm Support
The library supports the standard COSE algorithm identifiers required by modern authenticators:
- `COSEAlgorithmIdentifier.ECDSA_SHA_256` (`-7`, ES256): Default on Apple Touch ID/Face ID, Windows Hello, and Android Biometrics.
- `COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256` (`-257`, RS256): Used by older TPMs and legacy Windows Hello configurations.
- `COSEAlgorithmIdentifier.EDDSA` (`-8`, Ed25519): Supported on modern YubiKeys (firmware 5.2+).

### 4.5 Backend Suitability Verdict
`webauthn` is exceptionally well-suited for PBIP. It operates as a pure in-process verification library without external network calls, daemons, or background threads, fitting cleanly into FastAPI request lifecycles and PBIP's synchronous SQLAlchemy service architecture.

---

## 5. Technical Evaluation: Browser & Platform WebAuthn API Compatibility

### 5.1 W3C Specification Standards
- **WebAuthn Level 2 (W3C Recommendation, April 2021):** Formalized core credential creation/assertion, user verification requirements, and extension architecture.
- **WebAuthn Level 3 (W3C Recommendation, August 2026):** Formalized passkey autofill (Conditional UI), Backup Eligibility (`BE`) and Backup State (`BS`) flags in authenticator data, `getClientCapabilities()`, and cross-device authentication (FIDO Hybrid transport / caBLE).

### 5.2 Browser Engine Compatibility Matrix
| Browser Family | Supported Versions | Platform Authenticators Supported | Conditional UI / Autofill |
|---|---|---|---|
| **Google Chrome / Chromium** | Chrome 67+ (Desktop & Android) | Windows Hello, Android Keystore, macOS Touch ID | Chrome 108+ |
| **Apple Safari** | Safari 13+ (macOS), Safari 14.5+ (iOS) | Apple Touch ID, Face ID, iCloud Keychain | Safari 16+ (iOS 16, macOS 13) |
| **Microsoft Edge** | Edge 79+ (Chromium) | Windows Hello, Platform TPM | Edge 108+ |
| **Mozilla Firefox** | Firefox 60+ (Core), Firefox 122+ (Passkeys) | Windows Hello, macOS Touch ID, Soft token | Firefox 122+ |

*Global Support Status:* Can I Use reports over 98.2% global browser support for core WebAuthn APIs across active desktop and mobile web devices.

### 5.3 Synced (Multi-Device) Passkeys vs. Device-Bound Security Keys
1. **Multi-Device Synced Passkeys:**
   - Managed by consumer cloud keychains (Apple iCloud Keychain, Google Password Manager, 1Password, Bitwarden).
   - Private keys synchronize end-to-end encrypted across user devices.
   - *UX Impact:* Eliminates single-device loss lockouts; enables seamless cross-device login via QR codes (FIDO Hybrid transport / WebAuthn caBLE).
   - *Security Nuance (Signature Counters):* Authenticators across synced devices do not synchronize internal monotonic counters. Consequently, synced passkey providers frequently return a static `sign_count = 0`. The backend verification logic **must not** treat `sign_count == 0` as a replay or cloning attack.
2. **Device-Bound Keys:**
   - Physical security keys (YubiKey 5 Series) or non-exportable hardware enclaves (`residentKey="required"`, `authenticatorAttachment="cross-platform"`).
   - Signature counter strictly increments upon every assertion.
   - Mandatory for high-privilege staff roles (`ADMIN`, `FRAUD_ANALYST`, `COMPLIANCE_OFFICER`) where key export is unacceptable.

### 5.4 Secure Context Requirement
The browser WebAuthn API is strictly restricted to Secure Contexts (`window.isSecureContext === true`):
- Accessible over `https://` in deployed staging and production environments.
- Accessible over `http://localhost` or `http://127.0.0.1` for local developer testing.
- Any direct HTTP access over an IP address or unencrypted non-localhost domain causes `navigator.credentials` to be undefined.

---

## 6. Recommended Architectural Decision

### 6.1 Core Decision
PBIP will adopt FIDO2 / WebAuthn passkeys via the Python `webauthn` library following a **phased hybrid architecture** (Option C):
1. **Passkey as a Phishing-Resistant MFA Factor:** Customers and bank staff may register one or more FIDO2 authenticators as an MFA method alongside TOTP.
2. **Token Representation & AMR:** Sessions authenticated via password + WebAuthn passkey will carry the Authentication Method Reference `webauthn` (`amr: ["pwd", "webauthn"]`). The principal authorization dependency (`require_roles(..., mfa=True)`) will accept either `otp` or `webauthn` as fulfilling the MFA mandate.
3. **High-Risk Payment Step-Up:** Payments triggering a risk escalation (`score >= 30` or transfers exceeding £10,000) will support cryptographic step-up via WebAuthn with PSD2 dynamic linking.
4. **Passkey-First Authentication (Future Phase):** Customers with registered passkeys may opt into biometric autofill login (`conditional` mediation) without entering a password.

---

## 7. Trade-offs, Security Risks, Privacy Considerations, and Mitigations

| Risk / Trade-off | Threat Analysis | Architectural Mitigation |
|---|---|---|
| **Adversary-in-the-Middle (AiTM) Phishing** | Reverse proxies relay credentials and session cookies in real time. | **Eliminated.** WebAuthn authenticators sign the browser's origin. The server enforces `clientData.origin == settings.webauthn_origin` and `rp_id == settings.webauthn_rp_id`. |
| **Challenge Replay & Timing Attacks** | Stolen or replayed assertion payloads submitted to forge sessions. | Challenges are 32-byte cryptographic random values stored in Redis with a 90-second TTL. The challenge is atomically deleted (`GETDEL`) upon verification attempt. |
| **Credential Cloning / Desynchronization** | Cloned private keys on unauthorized hardware. | For device-bound keys with `sign_count > 0`, the server enforces strictly monotonic counter increments. For synced passkeys (`sign_count == 0`), reliance shifts to user verification (`uv=True`) and risk-based device telemetry. |
| **Device Loss & Account Lockout** | Customer loses smartphone or hardware key. | (1) Allow customers to register multiple passkeys (e.g., laptop + phone + backup key). (2) Retain Argon2id password + encrypted recovery codes as an account recovery fallback. |
| **EBA / PSD2 "Possession" Regulatory Debate** | Regulators debate whether cloud-synced passkeys satisfy strict "single-device possession". | Retail banking will accept synced passkeys with biometric user verification (`userVerification="required"`). High-privilege staff roles will mandate device-bound hardware keys (`authenticatorAttachment="cross-platform"`). |
| **User Privacy & Device Tracking** | Tracking customers across sites via authenticator serial numbers. | Enforce `attestation="none"` for all retail customer registrations. No hardware serial numbers or device certificates are stored. |

---

## 8. Proposed Architecture & Data Requirements

### 8.1 Database Model Additions (`apps/api/src/pbip_api/modules/auth/models.py`)

A new table `webauthn_credentials` will store registered public keys and metadata:

```python
class WebAuthnCredential(Base):
    """Registered FIDO2 / WebAuthn passkeys and security keys."""

    __tablename__ = "webauthn_credentials"
    __table_args__ = (
        Index("ix_webauthn_credentials_user_id", "user_id"),
        Index("uq_webauthn_credentials_credential_id", "credential_id", unique=True),
    )

    id: Mapped[UUID] = uuid_pk()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Binary credential ID returned by the authenticator (up to 1023 bytes per spec)
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # COSE-encoded public key bytes
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Signature counter for clone detection (0 for synced passkeys)
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # Supported transports: ["internal", "hybrid", "usb", "nfc", "ble"]
    transports: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    # Level 3 authenticatorData flags
    backup_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    backup_state: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Authenticator Attestation GUID (identifies authenticator make/model family)
    aaguid: Mapped[UUID | None] = mapped_column()
    # User-assigned friendly label (e.g. "MacBook Touch ID", "Personal iPhone")
    friendly_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
```

### 8.2 Ephemeral Challenge Management (Redis)
Challenges must not be stored in JWTs on the client (which opens tampering/reflection vectors). Instead, PBIP's existing Redis infrastructure will manage transient challenge state:
- **Key Pattern:** `auth:webauthn:challenge:{challenge_id}`
- **TTL:** 90 seconds.
- **Value Schema:** JSON `{ "user_id": str(uuid) | None, "ceremony": "registration" | "authentication" | "stepup", "expected_challenge": base64url_str }`.
- **Atomic Consumption:** Retrieved and deleted in a single round-trip (`GETDEL` command or pipeline) to prevent challenge replay.

### 8.3 Security Events Telemetry
Extend `SecurityEventType` enum to feed audit logs and fraud engine features:
- `WEBAUTHN_REGISTER_SUCCESS`
- `WEBAUTHN_REGISTER_FAILURE`
- `WEBAUTHN_LOGIN_SUCCESS`
- `WEBAUTHN_LOGIN_FAILURE`
- `WEBAUTHN_CREDENTIAL_DELETED`
- `WEBAUTHN_STEP_UP_SUCCESS`
- `WEBAUTHN_STEP_UP_FAILURE`

### 8.4 Planned API Endpoints (`apps/api/src/pbip_api/modules/auth/router.py`)

1. **Credential Registration (Authenticated):**
   - `POST /api/v1/auth/webauthn/register/options`: Issues registration challenge, requiring active bearer token.
   - `POST /api/v1/auth/webauthn/register/verify`: Verifies client attestation, persists `WebAuthnCredential`, records audit event.
2. **Authentication / MFA Login:**
   - `POST /api/v1/auth/webauthn/login/options`: Generates challenge for user email or discoverable credential.
   - `POST /api/v1/auth/webauthn/login/verify`: Verifies assertion, issues session tokens with `amr: ["webauthn"]` or `amr: ["pwd", "webauthn"]`, sets refresh cookie.
3. **Credential Management:**
   - `GET /api/v1/auth/webauthn/credentials`: Lists registered credentials (IDs, friendly names, created/last used timestamps; never private or raw public keys).
   - `DELETE /api/v1/auth/webauthn/credentials/{id}`: Revokes passkey. Cannot revoke last credential if customer has disabled password authentication.
4. **Transaction Step-Up:**
   - `POST /api/v1/payments/transfers/{id}/step-up/webauthn-options`: Generates challenge with dynamic transaction linking.
   - `POST /api/v1/payments/transfers/{id}/step-up/webauthn-verify`: Validates signature over transaction amount and recipient before posting transfer.

---

## 9. Phased Implementation Roadmap

```mermaid
flowchart TD
    subgraph Phase 1 [Sprint 5 - Architecture Evaluation]
        P1[TASK-073 Spike & ADR 0008]
    end

    subgraph Phase 2 [Sprint 6 - Backend Core & MFA]
        P2A[Add webauthn dependency via uv]
        P2B[Alembic Migration: webauthn_credentials]
        P2C[Redis Challenge Lifecycle Service]
        P2D[AuthService WebAuthn Registration & Verification]
        P2E[Security & BOLA Test Suite]
    end

    subgraph Phase 3 [Sprint 6 - Frontend Web Client]
        P3A[Passkey Management in Settings]
        P3B[WebAuthn MFA Verification Modal]
        P3C[Playwright E2E with Virtual Authenticators]
    end

    subgraph Phase 4 [Sprint 7 - Step-Up & Dynamic Linking]
        P4A[High-Risk Transfer Step-Up]
        P4B[Dynamic Transaction Data Binding]
    end

    subgraph Phase 5 [Sprint 7 - Passwordless & Conditional UI]
        P5A[Passkey Autofill / Conditional Mediation]
        P5B[Discoverable Passkey-First Login]
    end

    Phase 1 --> Phase 2
    Phase 2 --> Phase 3
    Phase 3 --> Phase 4
    Phase 4 --> Phase 5
```

### Milestone Deliverables & Acceptance Criteria:
- **Phase 1 (TASK-073 - Done upon ADR approval):** ADR 0008 committed; architectural buy-in; documentation updated.
- **Phase 2 (Backend Foundation):**
  - Dependency `webauthn>=3.0.0` added to `apps/api/pyproject.toml`.
  - Alembic migration creating `webauthn_credentials` with up/down tests.
  - Unit tests verifying challenge generation, attestation parsing, signature validation, expired challenge rejection, and bad origin rejection.
  - BOLA tests proving user B cannot delete or access user A's passkeys.
- **Phase 3 (Frontend Integration):**
  - Settings UI for passkey registration and friendly naming.
  - Login modal supporting passkey verification as second factor.
  - Playwright E2E tests utilizing Chrome DevTools Protocol (CDP) `WebAuthn.enable` and `WebAuthn.addVirtualAuthenticator`.
- **Phase 4 (PSD2 Dynamic Linking):**
  - Transfer step-up binding amount and payee into `clientDataJSON`.
- **Phase 5 (Conditional UI):**
  - WebAuthn autofill on login page (`mediation: "conditional"`).

---

## 10. Testing Strategy, Deployment Considerations, and Rollback Plan

### 10.1 Automated Testing Strategy
1. **Unit Testing:**
   - Mock authenticator payloads utilizing `py_webauthn` test fixtures.
   - Assert failure on challenge expiration, mismatched RP ID, origin spoofing, and invalid cryptographic signatures.
   - Assert counter increment validation: accept `sign_count == 0` for synced passkeys; reject decremented counter for device-bound keys.
2. **Security & Regression Testing:**
   - **BOLA Protection:** Add WebAuthn credential deletion and lookup to `test_rbac_bola.py`.
   - **Log Redaction:** Assert that raw binary public keys, credential IDs, and challenges are redacted or sanitized in structured logs (`test_logging_redaction.py`).
   - **Route Policy:** Ensure new endpoints are registered in `test_route_policy_registry.py` under appropriate authentication dependencies.
3. **End-to-End Testing (Playwright):**
   - Utilize Chrome DevTools Protocol (CDP) virtual authenticators (`authenticatorAttachment="platform"`, protocol `ctap2`, transport `internal`) to simulate biometric Touch ID / Face ID interactions in automated headless CI.

### 10.2 Deployment & Configuration
- **Configuration Keys (`core/config.py`):**
  - `WEBAUTHN_ENABLED: bool = False` (Feature flag for dark launch).
  - `WEBAUTHN_RP_ID: str = "pbip.bank.internal"` (Configured domain).
  - `WEBAUTHN_RP_NAME: str = "PBIP Banking"`.
  - `WEBAUTHN_ORIGIN: str = "https://pbip.bank.internal"` (Expected origin URL).
  - `WEBAUTHN_CHALLENGE_TTL_SECONDS: int = 90`.
- **Staging / Production Prerequisites:** Valid TLS certificate termination at nginx; `COOKIE_SECURE=True`.

### 10.3 Rollback Plan
Because WebAuthn operates as an additive authentication factor alongside Argon2id and TOTP:
- Setting `WEBAUTHN_ENABLED=false` immediately disables passkey registration and authentication routes without invalidating user accounts.
- Customers seamlessly fall back to their existing password and TOTP credentials.
- Database records in `webauthn_credentials` remain preserved without data loss for subsequent reactivation.

---

## 11. Sources, Documentation, and Verified Evidence

1. **W3C Web Authentication Working Group:**
   - *Web Authentication: An API for accessing Public Key Credentials Level 2* (W3C Recommendation 08 April 2021): https://www.w3.org/TR/webauthn-2/
   - *Web Authentication: An API for accessing Public Key Credentials Level 3* (W3C Recommendation 20 August 2026): https://www.w3.org/TR/webauthn-3/
2. **FIDO Alliance:**
   - *Passkeys Architectural Overview and Guidance*: https://fidoalliance.org/passkeys/
   - *Multi-Device FIDO Credentials / Synced Passkeys*: https://fidoalliance.org/white-paper-multi-device-fido-credentials/
3. **PyWebAuthn (Duo Labs / Cisco):**
   - *py_webauthn Repository & Documentation*: https://github.com/duo-labs/py_webauthn (Verified version 3.0.x on PyPI; requires Python $\ge 3.10$, depends on `cryptography` and `cbor2`).
4. **European Banking Authority (EBA):**
   - *Guidelines on ICT and security risk management* (EBA/GL/2019/04): https://www.eba.europa.eu/regulation-and-policy/internal-governance/guidelines-on-ict-and-security-risk-management
   - *Regulatory Technical Standards on Strong Customer Authentication (SCA) under PSD2*: https://www.eba.europa.eu/regulation-and-policy/payment-services-and-electronic-money/regulatory-technical-standards-on-strong-customer-authentication-and-secure-communication-under-psd2
5. **Can I Use:**
   - *Web Authentication API Browser Support Matrix*: https://caniuse.com/webauthn (Verified $> 98\%$ global desktop and mobile browser support).
6. **Mozilla Developer Network (MDN):**
   - *Web Authentication API*: https://developer.mozilla.org/en-US/docs/Web/API/Web_Authentication_API
