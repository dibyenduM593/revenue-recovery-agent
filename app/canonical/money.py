from pydantic import BaseModel, field_validator

# ISO 4217 minor-unit exponent per currency: how many digits amount_minor
# carries past the decimal point. Absent currencies default to 2 (the ISO
# default for currencies not listed with a different exponent), not
# because 2 is universally correct, but because guessing wrong on an
# unlisted currency is a formatting bug, not the more expensive kind of
# wrong (an actual money value never comes from this table, only display).
CURRENCY_EXPONENTS: dict[str, int] = {
    "JPY": 0,
    "KRW": 0,
    "VND": 0,
    "BHD": 3,
    "KWD": 3,
    "OMR": 3,
    "JOD": 3,
    "INR": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
}
DEFAULT_EXPONENT = 2


class Money(BaseModel):
    amount_minor: int
    currency: str

    @field_validator("currency")
    @classmethod
    def currency_is_iso_code(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError(f"currency must be a 3-letter ISO 4217 code, got {value!r}")
        return value

    def __str__(self) -> str:
        exponent = CURRENCY_EXPONENTS.get(self.currency, DEFAULT_EXPONENT)
        major = self.amount_minor / (10**exponent)
        return f"{major:.{exponent}f} {self.currency}"

    def __add__(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise ValueError(f"cannot add {self.currency} to {other.currency}")
        return Money(amount_minor=self.amount_minor + other.amount_minor, currency=self.currency)
