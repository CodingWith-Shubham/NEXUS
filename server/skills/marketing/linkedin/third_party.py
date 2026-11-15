"""A third-party LinkedIn provider: a documented stub. NOT implemented, on purpose.

LinkedIn's official API lets an app post (and, where permitted, comment and react)
for the member who connected. It does not offer, to apps like this one:

- reading the member's feed or other people's posts (``r_member_social`` is
  restricted to approved developers),
- sending direct messages,
- sending connection requests.

Some commercial services offer those through their own platforms. If you choose
one, implement it here behind the same interface (``create_post``, ``comment``,
``react``) plus whatever extra methods it supports, select it with
``[linkedin] provider = "third_party"``, and keep the approval gate on every
action that publishes or contacts someone.

Rules for whoever builds it: only use the vendor's documented API with its own
credentials. Never call private LinkedIn endpoints, never automate a browser,
never use a member's session cookies. Each of those breaks LinkedIn's User
Agreement and puts the member's account at risk.
"""

from skills.marketing.linkedin import LinkedInError, Published

NOT_BUILT = (
    "The third-party LinkedIn provider isn't built. JARVIS uses LinkedIn's official API "
    "([linkedin] provider = \"official\")."
)


class ThirdPartyLinkedIn:
    name = "third_party"

    async def create_post(self, token: str, *, author: str, text: str) -> Published:
        raise LinkedInError(NOT_BUILT, kind="denied")

    async def comment(self, token: str, *, actor: str, target: str, text: str) -> Published:
        raise LinkedInError(NOT_BUILT, kind="denied")

    async def react(self, token: str, *, actor: str, target: str, reaction: str) -> Published:
        raise LinkedInError(NOT_BUILT, kind="denied")
