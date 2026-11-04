"""Customer and device use cases."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Boolean, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pbip_api.core.calendar import bank_today
from pbip_api.core.config import Settings
from pbip_api.core.crypto import CryptoError, FieldCipher
from pbip_api.core.db import utcnow
from pbip_api.core.errors import UnprocessableError
from pbip_api.modules.auth.models import User
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.customers.demographics import (
    CONSENT_PURPOSE,
    DEMOGRAPHICS_POLICY_VERSION,
    MAX_AGE,
    MIN_AGE,
    AgeBand,
    age_band,
    is_allowed_date_of_birth,
)
from pbip_api.modules.customers.models import Customer, Device
from pbip_api.modules.customers.risk import (
    MAX_SCORE,
    QUESTIONNAIRE_VERSION,
    RiskAnswers,
    RiskBand,
    score_answers,
)
from pbip_api.modules.customers.schemas import (
    CustomerProfileOut,
    DemographicsOut,
    DeviceOut,
    RiskProfileOut,
    RiskQuestionnaire,
)
from pbip_api.modules.notifications import service as notifications


def mask_phone_number(phone: str) -> str:
    """Mask phone number revealing only the last 4 digits."""
    cleaned = phone.strip()
    if len(cleaned) <= 4:
        return cleaned
    return "•" * (len(cleaned) - 4) + cleaned[-4:]


def to_device_out(device: Device, now: datetime | None = None) -> DeviceOut:
    current = now or utcnow()
    age_days = max(0, (current - device.first_seen_at).days)
    return DeviceOut(
        id=device.id,
        label=device.label,
        first_seen_at=device.first_seen_at,
        last_seen_at=device.last_seen_at,
        trusted=device.trusted,
        device_age_days=age_days,
    )


def recommendation_age_band(session: Session, customer_id: UUID, today: date) -> AgeBand:
    """The only way another module learns anything about a customer's age (ADR-0015).

    Returns the derived band when the customer has BOTH a date of birth and a recorded consent,
    and ``unknown`` otherwise. The date itself never leaves this module.

    ``session.get``: on a customer request the row is already in the session (the authenticated
    customer was just loaded), so this costs no query (#343).
    """
    subject = recommendation_subject(session, customer_id, today)
    return AgeBand.UNKNOWN if subject is None else subject.age_band


@dataclass(frozen=True, slots=True)
class RecommendationSubject:
    """What the recommendation engine may know about the customer row (ADR-0015, ADR-0056)."""

    age_band: AgeBand
    risk_band: RiskBand | None  # RTQ-1 (TASK-097); None until assessed
    user_id: UUID  # for the loans module's lockout rule (UC-06), never shown or sent to the engine


def recommendation_subject(
    session: Session, customer_id: UUID, today: date
) -> RecommendationSubject | None:
    """The age band (rules of :func:`recommendation_age_band`), the RTQ-1 risk band and the user id
    from ONE read of the customer row, for the recommendation engine (ADR-0056). One read matters
    off the HTTP path: nothing else holds the row there, so two separate ``session.get`` calls would
    each load it. ``None`` when there is no such customer."""
    customer = session.get(Customer, customer_id)
    if customer is None:
        return None
    risk = RiskBand(customer.risk_profile) if customer.risk_profile else None
    age = (
        AgeBand.UNKNOWN
        if customer.recommendations_consent_at is None
        else age_band(customer.date_of_birth, today)
    )
    return RecommendationSubject(age, risk, customer.user_id)


def risk_profile(session: Session, customer_id: UUID) -> RiskProfileOut:
    """The customer's own risk profile for other modules (TASK-105: assistant suggestions).

    The caller passes the AUTHENTICATED customer's id; the band is read, never derived again.
    """
    return _risk_profile_out(session.get_one(Customer, customer_id))


def _risk_profile_out(customer: Customer) -> RiskProfileOut:
    return RiskProfileOut(
        band=RiskBand(customer.risk_profile) if customer.risk_profile else None,
        score=customer.risk_score,
        max_score=MAX_SCORE,
        answers=(
            RiskQuestionnaire.model_validate(customer.risk_answers)
            if customer.risk_answers is not None
            else None
        ),
        questionnaire_version=customer.risk_questionnaire_version,
        assessed_at=customer.risk_assessed_at,
    )


def _demographics_out(customer: Customer, today: date) -> DemographicsOut:
    return DemographicsOut(
        date_of_birth=customer.date_of_birth,
        age_band=age_band(customer.date_of_birth, today),
        recommendations_consent=customer.recommendations_consent_at is not None,
        consent_granted_at=customer.recommendations_consent_at,
        consent_policy_version=customer.recommendations_consent_policy,
        current_policy_version=DEMOGRAPHICS_POLICY_VERSION,
    )


class CustomerService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.audit = AuditService(session)

    def _get_field_cipher(self) -> FieldCipher | None:
        try:
            key = self.settings.field_encryption_key.get_secret_value()
            if key:
                return FieldCipher(key)
        except (CryptoError, ValueError):
            pass
        return None

    def get_profile(self, customer: Customer) -> CustomerProfileOut:
        """Return authenticated customer profile with masked phone number."""
        email = self.session.execute(
            select(User.email).where(User.id == customer.user_id)
        ).scalar_one_or_none()

        phone_masked: str | None = None
        if customer.phone_enc:
            cipher = self._get_field_cipher()
            if cipher:
                try:
                    decrypted_phone = cipher.decrypt(customer.phone_enc)
                    phone_masked = mask_phone_number(decrypted_phone)
                except CryptoError:
                    phone_masked = None

        return CustomerProfileOut(
            id=customer.id,
            user_id=customer.user_id,
            email=email,
            full_name=customer.full_name,
            date_of_birth=customer.date_of_birth,
            phone_masked=phone_masked,
            kyc_status=customer.kyc_status.value,
            created_at=customer.created_at,
        )

    def register_device(
        self,
        customer: Customer,
        raw_fingerprint: str,
        label: str | None,
        meta: RequestMeta,
    ) -> tuple[Device, bool]:
        """Record or touch a device fingerprint.

        Computes SHA-256 fingerprint hash. New devices default to untrusted.
        Repeat registrations touch last_seen_at while preserving first_seen_at and trusted status.
        Never logs or exposes the raw fingerprint.
        """
        fingerprint_hash = hashlib.sha256(raw_fingerprint.encode("utf-8")).hexdigest()
        clean_label = label.strip() if label else None

        # One atomic upsert instead of select-then-insert: concurrent first registrations of the
        # same device used to race on uq_devices_customer_fingerprint (HTTP 500). `xmax = 0` is
        # true only for a freshly inserted row, so callers still learn "new" vs "touched".
        insert = pg_insert(Device).values(
            customer_id=customer.id,
            fingerprint_hash=fingerprint_hash,
            label=clean_label,
            trusted=False,
        )
        upsert = insert.on_conflict_do_update(
            constraint="uq_devices_customer_fingerprint",
            set_={
                "last_seen_at": func.now(),
                "label": func.coalesce(insert.excluded.label, Device.label),
            },
        )
        inserted = literal_column("(xmax = 0)", Boolean).label("inserted")
        row = self.session.execute(upsert.returning(Device.id, inserted)).one()
        device_id, is_new = row.id, bool(row.inserted)
        device = self.session.get_one(Device, device_id, populate_existing=True)

        self.audit.record(
            actor_user_id=customer.user_id,
            action="DEVICE_REGISTERED" if is_new else "DEVICE_TOUCHED",
            resource_type="device",
            resource_id=str(device.id),
            ip=meta.ip,
            request_id=meta.request_id,
            details={
                "trusted": device.trusted,
                "is_new": is_new,
            },
        )
        if is_new:
            notifications.enqueue_security(
                self.session, notifications.DEVICE_REGISTERED, customer.user_id
            )
        self.session.commit()
        return device, is_new

    def list_devices(self, customer_id: UUID) -> list[Device]:
        """List active devices owned by the caller."""
        return list(
            self.session.execute(
                select(Device)
                .where(Device.customer_id == customer_id)
                .order_by(Device.last_seen_at.desc())
            )
            .scalars()
            .all()
        )

    def _lock(self, customer: Customer) -> Customer:
        """Re-read the caller's row FOR UPDATE so concurrent edits serialise (no lost update,
        and the no-op checks below see the committed state)."""
        return self.session.execute(
            select(Customer)
            .where(Customer.id == customer.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()

    def _audit_customer(
        self, customer: Customer, action: str, meta: RequestMeta, details: dict[str, object]
    ) -> None:
        self.audit.record(
            actor_user_id=customer.user_id,
            action=action,
            resource_type="customer",
            resource_id=str(customer.id),
            ip=meta.ip,
            request_id=meta.request_id,
            details=details,
        )

    def set_phone(self, customer: Customer, phone: str, meta: RequestMeta) -> CustomerProfileOut:
        """Store the customer's mobile number encrypted with ``FieldCipher`` (ADR-0042, #398).

        The plain number is never stored, logged or audited: the audit row records only THAT it
        changed. Saving the number already on file changes nothing and is not re-audited (Fernet
        is randomised, so the stored token is decrypted to compare).
        """
        cipher = self._require_field_cipher()
        row = self._lock(customer)
        current = self._decrypt_phone(cipher, row.phone_enc)
        if current != phone:
            replaced = row.phone_enc is not None
            row.phone_enc = cipher.encrypt(phone)
            self._audit_customer(row, "PHONE_SET", meta, {"replaced_existing": replaced})
        self.session.commit()
        return self.get_profile(row)

    def delete_phone(self, customer: Customer, meta: RequestMeta) -> None:
        """Erase the mobile number on request; nothing else about the customer changes."""
        row = self._lock(customer)
        if row.phone_enc is not None:
            row.phone_enc = None
            self._audit_customer(row, "PHONE_DELETED", meta, {})
        self.session.commit()

    def _require_field_cipher(self) -> FieldCipher:
        cipher = self._get_field_cipher()
        if cipher is None:
            # Staging/production refuse to boot without the key; this guards a misconfigured dev
            # stack from ever storing a phone number in clear (generic 500, nothing written).
            raise RuntimeError("FIELD_ENCRYPTION_KEY is not configured")
        return cipher

    @staticmethod
    def _decrypt_phone(cipher: FieldCipher, token: str | None) -> str | None:
        if token is None:
            return None
        try:
            return cipher.decrypt(token)
        except CryptoError:
            return None  # unreadable under the current keys: overwrite it

    def get_risk_profile(self, customer: Customer) -> RiskProfileOut:
        return _risk_profile_out(customer)

    def assess_risk_profile(
        self, customer: Customer, questionnaire: RiskQuestionnaire, now: datetime, meta: RequestMeta
    ) -> RiskProfileOut:
        """Score the answers and store band, score, answers and time together (TASK-097).

        Every submission is a new assessment, so it is always stored and audited, even when the
        band is unchanged: the assessment date is what a suitability check relies on.
        """
        answers = RiskAnswers(**questionnaire.model_dump())
        assessment = score_answers(answers)
        row = self._lock(customer)
        previous = row.risk_profile
        row.risk_profile = assessment.band.value
        row.risk_score = assessment.score
        row.risk_answers = answers.as_dict()
        row.risk_questionnaire_version = QUESTIONNAIRE_VERSION
        row.risk_assessed_at = now
        self._audit_customer(
            row,
            "RISK_PROFILE_ASSESSED",
            meta,
            {
                "band": assessment.band.value,
                "previous_band": previous,
                "score": assessment.score,
                "questionnaire_version": QUESTIONNAIRE_VERSION,
                "answers": answers.as_dict(),
            },
        )
        self.session.commit()
        return _risk_profile_out(row)

    def get_demographics(self, customer: Customer, today: date) -> DemographicsOut:
        return _demographics_out(customer, today)

    def set_date_of_birth(
        self, customer: Customer, date_of_birth: date, now: datetime, meta: RequestMeta
    ) -> DemographicsOut:
        """Store the self-declared date of birth. The audit row records THAT it changed and the
        policy version, never the date itself (the audit trail is visible to staff)."""
        today = bank_today(now)
        if not is_allowed_date_of_birth(date_of_birth, today):
            raise UnprocessableError(
                f"date of birth must give an age between {MIN_AGE} and {MAX_AGE}"
            )
        row = self._lock(customer)
        if row.date_of_birth != date_of_birth:
            replaced = row.date_of_birth is not None
            row.date_of_birth = date_of_birth
            self._audit_customer(
                row,
                "DATE_OF_BIRTH_SET",
                meta,
                {"policy_version": DEMOGRAPHICS_POLICY_VERSION, "replaced_existing": replaced},
            )
        self.session.commit()
        return _demographics_out(row, today)

    def delete_date_of_birth(self, customer: Customer, meta: RequestMeta) -> None:
        """Erase the date of birth on request (ADR-0015 rule 7); the consent record is kept."""
        row = self._lock(customer)
        if row.date_of_birth is not None:
            row.date_of_birth = None
            self._audit_customer(
                row, "DATE_OF_BIRTH_DELETED", meta, {"policy_version": DEMOGRAPHICS_POLICY_VERSION}
            )
        self.session.commit()

    def set_recommendations_consent(
        self, customer: Customer, granted: bool, now: datetime, meta: RequestMeta
    ) -> DemographicsOut:
        """Grant or withdraw the separate ADR-0015 consent. Withdrawal takes effect at once:
        ``recommendation_age_band`` reads this column on every recommendation request.
        Re-granting under the same policy version changes nothing and is not re-audited."""
        row = self._lock(customer)
        already = (
            row.recommendations_consent_policy == DEMOGRAPHICS_POLICY_VERSION
            if granted
            else row.recommendations_consent_at is None
        )
        if not already:
            action = "GRANTED" if granted else "WITHDRAWN"
            row.recommendations_consent_at = now if granted else None
            row.recommendations_consent_policy = DEMOGRAPHICS_POLICY_VERSION if granted else None
            self._audit_customer(
                row,
                f"RECOMMENDATIONS_CONSENT_{action}",
                meta,
                {"policy_version": DEMOGRAPHICS_POLICY_VERSION, "purpose": CONSENT_PURPOSE},
            )
        self.session.commit()
        return _demographics_out(row, bank_today(now))
