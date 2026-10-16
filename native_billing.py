"""Effective native prices shared by menus, invoices, precheckout and Core exports.

Supply's existing Stars tariff derives from rubles. The legacy configured Stars
map is not used by native checkout and must not determine cross-product credit.
"""


def stars_amount_for_plan(settings, plan_id: str) -> int:
    price_minor = int(settings.PREMIUM_PRICES_RUB.get(plan_id, 0) or 0)
    if price_minor <= 0:
        return 0
    rub = round(price_minor / 100)
    return max(1, round(rub / 1.5))


def monthly_tariffs(settings) -> dict[str, int]:
    return {
        'RUB': int(settings.PREMIUM_PRICES_RUB['m1']),
        'XTR': stars_amount_for_plan(settings, 'm1'),
    }
