"""Обезличивание резюме до любого вызова модели."""

from rvm.privacy.mask import (
    Masked,
    PrivacyError,
    assert_clean,
    contact_hash,
    mask,
    swap_identity,
)

__all__ = ["Masked", "PrivacyError", "assert_clean", "contact_hash", "mask", "swap_identity"]
