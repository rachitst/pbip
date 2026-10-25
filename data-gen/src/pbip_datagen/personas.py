"""Customer personas drive income, spending mix and behaviour. Extend them here only."""

from dataclasses import dataclass
from typing import Literal

IncomePattern = Literal["salary", "freelance", "allowance", "business", "pension"]


@dataclass(frozen=True, slots=True)
class Persona:
    name: str
    share: float  # share of the customer population, all personas sum to 1.0
    monthly_income_inr: tuple[int, int]  # inclusive range
    savings_rate: tuple[float, float]
    has_emi_probability: float
    category_weights: dict[str, float]  # discretionary spend mix, sums to 1.0
    income_pattern: IncomePattern  # how and when income arrives (see calendar.py)
    discretionary_per_month: tuple[int, int]  # range of the Poisson mean of card/UPI spends
    monthly_churn_probability: float  # hazard of becoming inactive in any given month


PERSONAS: tuple[Persona, ...] = (
    Persona(
        name="salaried_professional",
        share=0.40,
        monthly_income_inr=(45_000, 180_000),
        savings_rate=(0.10, 0.35),
        has_emi_probability=0.55,
        category_weights={
            "groceries": 0.20,
            "dining": 0.15,
            "transport": 0.12,
            "shopping": 0.18,
            "entertainment": 0.10,
            "utilities": 0.15,
            "travel": 0.10,
        },
        income_pattern="salary",
        discretionary_per_month=(22, 40),
        monthly_churn_probability=0.004,
    ),
    Persona(
        name="student",
        share=0.15,
        monthly_income_inr=(8_000, 25_000),
        savings_rate=(0.0, 0.10),
        has_emi_probability=0.02,
        category_weights={
            "groceries": 0.18,
            "dining": 0.25,
            "transport": 0.12,
            "shopping": 0.15,
            "entertainment": 0.20,
            "utilities": 0.05,
            "travel": 0.05,
        },
        income_pattern="allowance",
        discretionary_per_month=(15, 35),
        monthly_churn_probability=0.015,
    ),
    Persona(
        name="freelancer",
        share=0.15,
        monthly_income_inr=(20_000, 150_000),
        savings_rate=(0.0, 0.30),
        has_emi_probability=0.25,
        category_weights={
            "groceries": 0.20,
            "dining": 0.15,
            "transport": 0.10,
            "shopping": 0.15,
            "entertainment": 0.10,
            "utilities": 0.20,
            "travel": 0.10,
        },
        income_pattern="freelance",
        discretionary_per_month=(18, 36),
        monthly_churn_probability=0.008,
    ),
    Persona(
        name="business_owner",
        share=0.15,
        monthly_income_inr=(80_000, 500_000),
        savings_rate=(0.05, 0.40),
        has_emi_probability=0.60,
        category_weights={
            "groceries": 0.12,
            "dining": 0.18,
            "transport": 0.10,
            "shopping": 0.20,
            "entertainment": 0.10,
            "utilities": 0.12,
            "travel": 0.18,
        },
        income_pattern="business",
        discretionary_per_month=(25, 45),
        monthly_churn_probability=0.005,
    ),
    Persona(
        name="retiree",
        share=0.15,
        monthly_income_inr=(25_000, 90_000),
        savings_rate=(0.05, 0.25),
        has_emi_probability=0.10,
        category_weights={
            "groceries": 0.30,
            "dining": 0.08,
            "transport": 0.07,
            "shopping": 0.10,
            "entertainment": 0.05,
            "utilities": 0.30,
            "travel": 0.10,
        },
        income_pattern="pension",
        discretionary_per_month=(8, 18),
        monthly_churn_probability=0.003,
    ),
)
