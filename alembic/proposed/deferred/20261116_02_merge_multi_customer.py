"""merge_multi_customer (PROPOSED, deferred; schema-free merge)

Revision ID: 20261116_02
Revises: 20261111_05, 20261116_04
Create Date: 2026-11-16

Day 54B (decision R4). ``20261111_05`` keeps its original parent ``20261111_06``: it has
been applied (the guarded test lifecycle runs it), so its history is not rewritten. The
active chain stays linear (``20261111_06 -> 20261115_01 (K2) -> 20261116_01 (K3) -> 20261116_03 (K4) -> 20261116_04 (K5)``) and this
revision joins the two heads for the NEXT contract only. It changes no schema object.
"""

from typing import Sequence, Union

revision: str = "20261116_02"
down_revision: Union[str, Sequence[str], None] = ("20261111_05", "20261116_04")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
