"""Customer and device use cases."""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings
from pbip_api.core.crypto import CryptoError, FieldCipher
from pbip_api.core.db import utcnow
from pbip_api.modules.auth.models import User
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.customers.models import Customer, Device
from pbip_api.modules.customers.schemas import CustomerProfileOut, DeviceOut
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
