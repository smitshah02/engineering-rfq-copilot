"""Trusted manifest of approved business-rule documents: the trust anchor for rule content.

Each entry is (rule_kind, source file sha256, canonical document fingerprint). The source
sha256 is provenance: the hash of the frozen dataset file the document was seeded from.
The fingerprint is canonical.fingerprint() of that file parsed with exact Decimals, so it
can be re-derived from the frozen file at any time and compared with what the database
returns. A database row is trusted only when both values match this version-controlled
file; changing the JSONB content while leaving the stored sha256 alone is detected.

Registering a new rule version means adding an entry here through code review, with a
test that re-derives both values from the approved source. There is no other path.
"""

from types import MappingProxyType

TRUSTED_RULES = MappingProxyType(
    {
        "mfg-v1.0": (
            "manufacturing",
            "a865aa138d3883beed4fae70c6b5218ae5f293dd0869457219688d02b4c46649",
            "1283cd5e993b4809c4fb9ecd75c120e5c3c9c4cd4d872ecc76a326e45a60eb8b",
        ),
        "price-v1.0": (
            "pricing",
            "17ab8a7e8c21cdc85adb60d292b8afca1decffbcf604982d75ba1aa306f24df7",
            "828deb2f5cf5234e042d756317aa31db257a8621bb2bac1d8268f76eea97df69",
        ),
        "approval-v1.1": (
            "approval",
            "7a40337f68766232706701f2755ff0771d07a78c70370221dd7a91556e4a56ef",
            "623aa0adc96ff5779d102f9006b0734f8013eb7de1aea54d35b8b8e681b8bdbd",
        ),
        # Not in dataset v1.1.0: approved 2026-11-09 (TD-004), source
        # src/rfq_copilot/rules/approval-v1.2.json, registered by seeding/rules.py.
        "approval-v1.2": (
            "approval",
            "6864a140bf5f5605493e677dfc2db6a8481e8cb3abfb4936782a4c7adf09aacc",
            "33b8ea368853860316b89698e468e53890ef81dc9caa8567bc34fd864c7d0ead",
        ),
        "workflow-v1.0": (
            "workflow",
            "7d0489f38690ce80f836692ae1e09c35ea8c59b0b1597ab51b83e4f680549906",
            "7e30e224edcc4b3bc46c2bf7cffde5c94423b75e66576da1c82f7d551215a1b2",
        ),
        "entity_contract@sha256:ab3276b33cbd72de59fe2e1f41a95b6944b7c834f82e5225adb9a4649e3b63a1": (
            "entity_contract",
            "ab3276b33cbd72de59fe2e1f41a95b6944b7c834f82e5225adb9a4649e3b63a1",
            "910d7ab93a4d22590309b8eda966298e8bf487c6e399ac4a208925b797bcf436",
        ),
        "tool_contract@sha256:341adfd52eafd6f076bf7e87f3e6bb01b9ebae66c8d16aef51c6e3a8e211cb37": (
            "tool_contract",
            "341adfd52eafd6f076bf7e87f3e6bb01b9ebae66c8d16aef51c6e3a8e211cb37",
            "9d27b08c057c72b946fe492c1c01a9519123f43c0ebe95b108256675df43884d",
        ),
        "business_constraints@sha256:91ba550ba7d5e5acbd4ca34f6e1f427a8aef1300cba1fda484f184eae9b2ea93": (
            "business_constraints",
            "91ba550ba7d5e5acbd4ca34f6e1f427a8aef1300cba1fda484f184eae9b2ea93",
            "ab26f03d68417a287fdc3f5b88ded6777d5ef44b340d20220b55317d6b7fc637",
        ),
        "coordination@sha256:04498e3780c3562f90c5fa3c89f7430cb19767d93e099a6379ca58b1c214fc89": (
            "coordination",
            "04498e3780c3562f90c5fa3c89f7430cb19767d93e099a6379ca58b1c214fc89",
            "9e05592535dfb8e034f464c5258f5f4116707a1044a732ae9c4c216d439164fa",
        ),
        "handoff@sha256:1c1ffddbcc80f22871fe7c438777a79b99146f08238951bc52f59685e33a32f8": (
            "handoff",
            "1c1ffddbcc80f22871fe7c438777a79b99146f08238951bc52f59685e33a32f8",
            "f425c04b13ba15290011a52a3e3a1c1b6994a8b0e54c5a86a615c7d6e6599cb5",
        ),
    }
)
