"""Lusha: not built yet. Selecting it (``[contacts] provider = "lusha"``) gives a clear message.

To build it: implement ``find_people`` with Lusha's prospecting search, and
``request_contact`` / ``parse_callback`` with its person enrichment API, keeping
the same ContactResult shape. Lookups already go through the approval gate.
"""

from skills.sales.contacts import ContactError, ContactRequest, ContactResult, Person

NOT_BUILT = "Lusha contact lookups aren't built yet. Use SignalHire ([contacts] provider)."


class LushaProvider:
    name = "lusha"

    def __init__(self, *, api_key: str | None = None):
        self._key = api_key

    async def find_people(self, *, company: str, titles: list[str], limit: int = 5) -> list[Person]:
        raise ContactError(NOT_BUILT)

    async def request_contact(self, person: Person, *, callback_url: str | None) -> ContactRequest:
        raise ContactError(NOT_BUILT)

    def parse_callback(self, payload, person: Person) -> ContactResult:
        return ContactResult(status="failed", error=NOT_BUILT)
