from pydantic import BaseModel, field_validator


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
        major = self.amount_minor / 100
        return f"{major:.2f} {self.currency}"

    def __add__(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise ValueError(f"cannot add {self.currency} to {other.currency}")
        return Money(amount_minor=self.amount_minor + other.amount_minor, currency=self.currency)
