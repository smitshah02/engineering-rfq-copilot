"""initial_rfq_database_schema

Revision ID: 20261108_01
Revises:
Create Date: 2026-11-08 16:06:32.454338

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from rfq_copilot.migration_safety import ensure_initial_state, ensure_test_downgrade

# revision identifiers, used by Alembic.
revision: str = "20261108_01"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    ensure_initial_state(op.get_bind())
    op.execute(sa.schema.CreateSchema("business"))
    op.execute(sa.schema.CreateSchema("orchestration"))
    op.execute("COMMENT ON SCHEMA business IS 'rfq-copilot:20261108_01'")
    op.execute("COMMENT ON SCHEMA orchestration IS 'rfq-copilot:20261108_01'")
    op.create_table(
        "business_rule_versions",
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("rule_kind", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column(
            "document", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(document) = 'object'",
            name=op.f("ck_business_rule_versions_document_shape"),
        ),
        sa.CheckConstraint(
            "rule_kind IN ('manufacturing','pricing','approval','workflow','entity_contract','tool_contract','business_constraints','coordination','handoff')",
            name=op.f("ck_business_rule_versions_rule_01"),
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_business_rule_versions_sha256_hex")
        ),
        sa.CheckConstraint(
            "length(btrim(rule_version)) > 0",
            name=op.f("ck_business_rule_versions_rule_version_nonblank"),
        ),
        sa.PrimaryKeyConstraint("rule_version", name=op.f("pk_business_rule_versions")),
        sa.UniqueConstraint(
            "rule_kind", "sha256", name=op.f("uq_business_rule_versions_rule_kind_sha256")
        ),
        schema="business",
    )
    op.create_table(
        "customers",
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("industry", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("account_status", sa.Text(), nullable=False),
        sa.Column("payment_terms", sa.Text(), nullable=False),
        sa.Column("credit_limit_usd", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("credit_exposure_usd", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("preferred_shipping", sa.Text(), nullable=False),
        sa.Column("restrictions", sa.Text(), nullable=False),
        sa.Column("created_date", sa.Date(), nullable=False),
        sa.Column("approval_requirement", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "account_status IN ('active','pending_verification','on_hold','credit_restricted','inactive')",
            name=op.f("ck_customers_rule_01"),
        ),
        sa.CheckConstraint(
            "approval_requirement IN ('threshold_based','commercial_review')",
            name=op.f("ck_customers_rule_03"),
        ),
        sa.CheckConstraint(
            "credit_exposure_usd <> 'NaN'::numeric",
            name=op.f("ck_customers_credit_exposure_usd_finite"),
        ),
        sa.CheckConstraint(
            "credit_limit_usd <> 'NaN'::numeric", name=op.f("ck_customers_credit_limit_usd_finite")
        ),
        sa.CheckConstraint(
            "credit_limit_usd >= 0 AND credit_exposure_usd >= 0", name=op.f("ck_customers_rule_02")
        ),
        sa.CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name=op.f("ck_customers_customer_id_identity"),
        ),
        sa.PrimaryKeyConstraint("customer_id", name=op.f("pk_customers")),
        schema="business",
    )
    op.create_table(
        "data_snapshots",
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("snapshot_kind", sa.Text(), nullable=False),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("artifact_ref", sa.Text(), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_data_snapshots_sha256_hex")),
        sa.CheckConstraint(
            "snapshot_kind IN ('inventory','capacity')", name=op.f("ck_data_snapshots_rule_01")
        ),
        sa.CheckConstraint(
            "length(btrim(dataset_version)) > 0",
            name=op.f("ck_data_snapshots_dataset_version_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(snapshot_id)) > 0 AND length(snapshot_id) <= 64",
            name=op.f("ck_data_snapshots_snapshot_id_identity"),
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_data_snapshots")),
        sa.UniqueConstraint(
            "snapshot_kind", "sha256", name=op.f("uq_data_snapshots_snapshot_kind_sha256")
        ),
        schema="business",
    )
    op.create_table(
        "operations",
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("is_quoted", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(operation_id)) > 0 AND length(operation_id) <= 64",
            name=op.f("ck_operations_operation_id_identity"),
        ),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("pk_operations")),
        schema="business",
    )
    op.create_table(
        "permissions",
        sa.Column("permission", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(permission)) > 0", name=op.f("ck_permissions_permission_nonblank")
        ),
        sa.PrimaryKeyConstraint("permission", name=op.f("pk_permissions")),
        schema="business",
    )
    op.create_table(
        "roles",
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role IN ('sales_representative','estimator','manufacturing_engineer','sales_manager','administrator','auditor')",
            name=op.f("ck_roles_rule_01"),
        ),
        sa.CheckConstraint("length(btrim(role)) > 0", name=op.f("ck_roles_role_nonblank")),
        sa.PrimaryKeyConstraint("role", name=op.f("pk_roles")),
        schema="business",
    )
    op.create_table(
        "suppliers",
        sa.Column("supplier_id", sa.Text(), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("normal_lead_business_days", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(supplier_id)) > 0 AND length(supplier_id) <= 64",
            name=op.f("ck_suppliers_supplier_id_identity"),
        ),
        sa.CheckConstraint("normal_lead_business_days >= 0", name=op.f("ck_suppliers_rule_01")),
        sa.PrimaryKeyConstraint("supplier_id", name=op.f("pk_suppliers")),
        schema="business",
    )
    op.create_table(
        "contacts",
        sa.Column("contact_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("contact_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(contact_id)) > 0 AND length(contact_id) <= 64",
            name=op.f("ck_contacts_contact_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name=op.f("ck_contacts_customer_id_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["business.customers.customer_id"],
            name=op.f("fk_contacts_customer_id_customers"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("contact_id", name=op.f("pk_contacts")),
        schema="business",
    )
    op.create_index(
        "ix_contacts_customer_name",
        "contacts",
        ["customer_id", "contact_name", "contact_id"],
        unique=False,
        schema="business",
        postgresql_using="btree",
    )
    op.create_index(
        "uq_contacts_primary",
        "contacts",
        ["customer_id"],
        unique=True,
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("is_primary"),
    )
    op.create_table(
        "finishes",
        sa.Column("finish_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("pricing_rule_version", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(finish_id)) > 0 AND length(finish_id) <= 64",
            name=op.f("ck_finishes_finish_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(pricing_rule_version)) > 0",
            name=op.f("ck_finishes_pricing_rule_version_nonblank"),
        ),
        sa.ForeignKeyConstraint(
            ["pricing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            name=op.f("fk_finishes_pricing_rule_version_business_rule_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("finish_id", name=op.f("pk_finishes")),
        schema="business",
    )
    op.create_table(
        "materials",
        sa.Column("material_id", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("grade", sa.Text(), nullable=False),
        sa.Column("density_kg_m3", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("cost_usd_per_kg", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("min_bend_radius_factor", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("availability_class", sa.Text(), nullable=False),
        sa.Column("pricing_rule_version", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "availability_class IN ('stocked','procure')", name=op.f("ck_materials_rule_02")
        ),
        sa.CheckConstraint(
            "category IN ('aluminum','mild_steel','stainless_steel','galvanized_steel','copper','brass')",
            name=op.f("ck_materials_rule_03"),
        ),
        sa.CheckConstraint(
            "cost_usd_per_kg <> 'NaN'::numeric", name=op.f("ck_materials_cost_usd_per_kg_finite")
        ),
        sa.CheckConstraint(
            "density_kg_m3 <> 'NaN'::numeric", name=op.f("ck_materials_density_kg_m3_finite")
        ),
        sa.CheckConstraint(
            "min_bend_radius_factor <> 'NaN'::numeric",
            name=op.f("ck_materials_min_bend_radius_factor_finite"),
        ),
        sa.CheckConstraint(
            "density_kg_m3 > 0 AND cost_usd_per_kg >= 0 AND min_bend_radius_factor > 0",
            name=op.f("ck_materials_rule_01"),
        ),
        sa.CheckConstraint(
            "length(btrim(material_id)) > 0 AND length(material_id) <= 64",
            name=op.f("ck_materials_material_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(pricing_rule_version)) > 0",
            name=op.f("ck_materials_pricing_rule_version_nonblank"),
        ),
        sa.ForeignKeyConstraint(
            ["pricing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            name=op.f("fk_materials_pricing_rule_version_business_rule_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("material_id", name=op.f("pk_materials")),
        schema="business",
    )
    op.create_table(
        "role_permissions",
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(permission)) > 0", name=op.f("ck_role_permissions_permission_nonblank")
        ),
        sa.CheckConstraint(
            "length(btrim(role)) > 0", name=op.f("ck_role_permissions_role_nonblank")
        ),
        sa.ForeignKeyConstraint(
            ["permission"],
            ["business.permissions.permission"],
            name=op.f("fk_role_permissions_permission_permissions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["role"],
            ["business.roles.role"],
            name=op.f("fk_role_permissions_role_roles"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("role", "permission", name=op.f("pk_role_permissions")),
        schema="business",
    )
    op.create_table(
        "users",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("assigned_region", sa.Text(), nullable=False),
        sa.Column("authentication_subject", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(authentication_subject)) > 0",
            name=op.f("ck_users_authentication_subject_nonblank"),
        ),
        sa.CheckConstraint("length(btrim(role)) > 0", name=op.f("ck_users_role_nonblank")),
        sa.CheckConstraint(
            "length(btrim(user_id)) > 0 AND length(user_id) <= 64",
            name=op.f("ck_users_user_id_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["role"],
            ["business.roles.role"],
            name=op.f("fk_users_role_roles"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_users")),
        sa.UniqueConstraint("authentication_subject", name=op.f("uq_users_authentication_subject")),
        schema="business",
    )
    op.create_index(
        "ix_users_reviewers",
        "users",
        ["role", "assigned_region", "user_id"],
        unique=False,
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "workcentres",
        sa.Column("workcentre_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("shift_calendar", sa.Text(), nullable=False),
        sa.Column("weekly_nominal_hours", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("downtime_hours_per_week", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("planning_horizon_weeks", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "downtime_hours_per_week <> 'NaN'::numeric",
            name=op.f("ck_workcentres_downtime_hours_per_week_finite"),
        ),
        sa.CheckConstraint(
            "weekly_nominal_hours <> 'NaN'::numeric",
            name=op.f("ck_workcentres_weekly_nominal_hours_finite"),
        ),
        sa.CheckConstraint(
            "length(btrim(operation_id)) > 0 AND length(operation_id) <= 64",
            name=op.f("ck_workcentres_operation_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(workcentre_id)) > 0 AND length(workcentre_id) <= 64",
            name=op.f("ck_workcentres_workcentre_id_identity"),
        ),
        sa.CheckConstraint("planning_horizon_weeks > 0", name=op.f("ck_workcentres_rule_02")),
        sa.CheckConstraint(
            "weekly_nominal_hours >= 0 AND downtime_hours_per_week >= 0 AND downtime_hours_per_week <= weekly_nominal_hours",
            name=op.f("ck_workcentres_rule_01"),
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["business.operations.operation_id"],
            name=op.f("fk_workcentres_operation_id_operations"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("workcentre_id", name=op.f("pk_workcentres")),
        sa.UniqueConstraint(
            "workcentre_id", "operation_id", name=op.f("uq_workcentres_workcentre_id_operation_id")
        ),
        schema="business",
    )
    op.create_table(
        "finish_material_categories",
        sa.Column("finish_id", sa.Text(), nullable=False),
        sa.Column("material_category", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "material_category IN ('aluminum','mild_steel','stainless_steel','galvanized_steel','copper','brass')",
            name=op.f("ck_finish_material_categories_rule_01"),
        ),
        sa.CheckConstraint(
            "length(btrim(finish_id)) > 0 AND length(finish_id) <= 64",
            name=op.f("ck_finish_material_categories_finish_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(material_category)) > 0",
            name=op.f("ck_finish_material_categories_material_category_nonblank"),
        ),
        sa.ForeignKeyConstraint(
            ["finish_id"],
            ["business.finishes.finish_id"],
            name=op.f("fk_finish_material_categories_finish_id_finishes"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint(
            "finish_id", "material_category", name=op.f("pk_finish_material_categories")
        ),
        schema="business",
    )
    op.create_table(
        "idempotency_records",
        sa.Column("receipt_id", sa.UUID(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column(
            "result_json",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column(
            "committed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(result_json) = 'object'",
            name=op.f("ck_idempotency_records_result_json_shape"),
        ),
        sa.CheckConstraint(
            "operation IN ('create_rfq','create_quote_draft','request_approval','approve_quote','reject_quote','release_quote','edit_quote_draft')",
            name=op.f("ck_idempotency_records_rule_01"),
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_idempotency_records_request_sha256_hex"),
        ),
        sa.CheckConstraint(
            "length(btrim(actor_id)) > 0 AND length(actor_id) <= 64",
            name=op.f("ck_idempotency_records_actor_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(correlation_id)) > 0",
            name=op.f("ck_idempotency_records_correlation_id_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name=op.f("ck_idempotency_records_customer_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name=op.f("ck_idempotency_records_idempotency_key_nonblank"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["business.users.user_id"],
            name=op.f("fk_idempotency_records_actor_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["business.customers.customer_id"],
            name=op.f("fk_idempotency_records_customer_id_customers"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("receipt_id", name=op.f("pk_idempotency_records")),
        sa.UniqueConstraint(
            "actor_id",
            "customer_id",
            "operation",
            "idempotency_key",
            name=op.f("uq_idempotency_records_actor_id_customer_id_operation_idempotency_key"),
        ),
        schema="business",
    )
    op.create_table(
        "material_finish_compatibility",
        sa.Column("material_id", sa.Text(), nullable=False),
        sa.Column("finish_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(finish_id)) > 0 AND length(finish_id) <= 64",
            name=op.f("ck_material_finish_compatibility_finish_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(material_id)) > 0 AND length(material_id) <= 64",
            name=op.f("ck_material_finish_compatibility_material_id_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["finish_id"],
            ["business.finishes.finish_id"],
            name=op.f("fk_material_finish_compatibility_finish_id_finishes"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["business.materials.material_id"],
            name=op.f("fk_material_finish_compatibility_material_id_materials"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint(
            "material_id", "finish_id", name=op.f("pk_material_finish_compatibility")
        ),
        schema="business",
    )
    op.create_table(
        "material_supported_operations",
        sa.Column("material_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(material_id)) > 0 AND length(material_id) <= 64",
            name=op.f("ck_material_supported_operations_material_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(operation_id)) > 0 AND length(operation_id) <= 64",
            name=op.f("ck_material_supported_operations_operation_id_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["business.materials.material_id"],
            name=op.f("fk_material_supported_operations_material_id_materials"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["business.operations.operation_id"],
            name=op.f("fk_material_supported_operations_operation_id_operations"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint(
            "material_id", "operation_id", name=op.f("pk_material_supported_operations")
        ),
        schema="business",
    )
    op.create_table(
        "material_thickness_options",
        sa.Column("material_id", sa.Text(), nullable=False),
        sa.Column("thickness_mm", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.CheckConstraint(
            "thickness_mm <> 'NaN'::numeric",
            name=op.f("ck_material_thickness_options_thickness_mm_finite"),
        ),
        sa.CheckConstraint(
            "length(btrim(material_id)) > 0 AND length(material_id) <= 64",
            name=op.f("ck_material_thickness_options_material_id_identity"),
        ),
        sa.CheckConstraint("thickness_mm > 0", name=op.f("ck_material_thickness_options_rule_01")),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["business.materials.material_id"],
            name=op.f("fk_material_thickness_options_material_id_materials"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint(
            "material_id", "thickness_mm", name=op.f("pk_material_thickness_options")
        ),
        schema="business",
    )
    op.create_table(
        "operation_routing",
        sa.Column("routing_id", sa.Text(), nullable=False),
        sa.Column("part_type", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("required_when", sa.Text(), nullable=False),
        sa.Column("workcentre_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "part_type IN ('flat_panel','mounting_plate','l_bracket','u_bracket','enclosure','fabricated_assembly')",
            name=op.f("ck_operation_routing_rule_03"),
        ),
        sa.CheckConstraint(
            "required_when IN ('always','finish_not_none')",
            name=op.f("ck_operation_routing_rule_02"),
        ),
        sa.CheckConstraint(
            "length(btrim(operation_id)) > 0 AND length(operation_id) <= 64",
            name=op.f("ck_operation_routing_operation_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(routing_id)) > 0 AND length(routing_id) <= 64",
            name=op.f("ck_operation_routing_routing_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(workcentre_id)) > 0 AND length(workcentre_id) <= 64",
            name=op.f("ck_operation_routing_workcentre_id_identity"),
        ),
        sa.CheckConstraint("sequence > 0", name=op.f("ck_operation_routing_rule_01")),
        sa.ForeignKeyConstraint(
            ["workcentre_id", "operation_id"],
            ["business.workcentres.workcentre_id", "business.workcentres.operation_id"],
            name=op.f("fk_operation_routing_workcentre_id_operation_id_workcentres"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("routing_id", name=op.f("pk_operation_routing")),
        sa.UniqueConstraint(
            "part_type", "sequence", name=op.f("uq_operation_routing_part_type_sequence")
        ),
        schema="business",
    )
    op.create_table(
        "production_capacity",
        sa.Column("capacity_id", sa.Text(), nullable=False),
        sa.Column("workcentre_id", sa.Text(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("nominal_hours", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("downtime_hours", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("scheduled_hours", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column(
            "available_hours",
            sa.Numeric(precision=12, scale=4),
            sa.Computed("nominal_hours - downtime_hours", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "remaining_hours",
            sa.Numeric(precision=12, scale=4),
            sa.Computed("nominal_hours - downtime_hours - scheduled_hours", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "utilization_fraction",
            sa.Numeric(precision=9, scale=4),
            sa.Computed(
                "CASE WHEN nominal_hours > downtime_hours THEN round(scheduled_hours / (nominal_hours - downtime_hours),4) ELSE NULL END",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("row_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(
            "available_hours <> 'NaN'::numeric",
            name=op.f("ck_production_capacity_available_hours_finite"),
        ),
        sa.CheckConstraint(
            "downtime_hours <> 'NaN'::numeric",
            name=op.f("ck_production_capacity_downtime_hours_finite"),
        ),
        sa.CheckConstraint(
            "nominal_hours <> 'NaN'::numeric",
            name=op.f("ck_production_capacity_nominal_hours_finite"),
        ),
        sa.CheckConstraint(
            "remaining_hours <> 'NaN'::numeric",
            name=op.f("ck_production_capacity_remaining_hours_finite"),
        ),
        sa.CheckConstraint(
            "scheduled_hours <> 'NaN'::numeric",
            name=op.f("ck_production_capacity_scheduled_hours_finite"),
        ),
        sa.CheckConstraint(
            "utilization_fraction <> 'NaN'::numeric",
            name=op.f("ck_production_capacity_utilization_fraction_finite"),
        ),
        sa.CheckConstraint(
            "EXTRACT(ISODOW FROM week_start) = 1", name=op.f("ck_production_capacity_rule_03")
        ),
        sa.CheckConstraint(
            "length(btrim(capacity_id)) > 0 AND length(capacity_id) <= 64",
            name=op.f("ck_production_capacity_capacity_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(snapshot_id)) > 0 AND length(snapshot_id) <= 64",
            name=op.f("ck_production_capacity_snapshot_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(workcentre_id)) > 0 AND length(workcentre_id) <= 64",
            name=op.f("ck_production_capacity_workcentre_id_identity"),
        ),
        sa.CheckConstraint(
            "nominal_hours >= 0 AND downtime_hours >= 0 AND downtime_hours <= nominal_hours",
            name=op.f("ck_production_capacity_rule_01"),
        ),
        sa.CheckConstraint("row_version >= 1", name=op.f("ck_production_capacity_rule_04")),
        sa.CheckConstraint(
            "scheduled_hours >= 0 AND scheduled_hours <= nominal_hours - downtime_hours",
            name=op.f("ck_production_capacity_rule_02"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["business.data_snapshots.snapshot_id"],
            name=op.f("fk_production_capacity_snapshot_id_data_snapshots"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["workcentre_id"],
            ["business.workcentres.workcentre_id"],
            name=op.f("fk_production_capacity_workcentre_id_workcentres"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("capacity_id", name=op.f("pk_production_capacity")),
        sa.UniqueConstraint(
            "workcentre_id",
            "week_start",
            name=op.f("uq_production_capacity_workcentre_id_week_start"),
        ),
        schema="business",
    )
    op.create_table(
        "rfqs",
        sa.Column("rfq_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("raw_request", sa.Text(), nullable=True),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column(
            "part_ids",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("row_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint("jsonb_typeof(part_ids) = 'array'", name=op.f("ck_rfqs_part_ids_shape")),
        sa.CheckConstraint(
            "status IN ('draft','intake','validated','blocked','closed','archived')",
            name=op.f("ck_rfqs_rule_01"),
        ),
        sa.CheckConstraint(
            "length(btrim(created_by)) > 0 AND length(created_by) <= 64",
            name=op.f("ck_rfqs_created_by_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name=op.f("ck_rfqs_customer_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64",
            name=op.f("ck_rfqs_rfq_id_identity"),
        ),
        sa.CheckConstraint("row_version >= 1", name=op.f("ck_rfqs_rule_02")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["business.users.user_id"],
            name=op.f("fk_rfqs_created_by_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["business.customers.customer_id"],
            name=op.f("fk_rfqs_customer_id_customers"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("rfq_id", name=op.f("pk_rfqs")),
        schema="business",
    )
    op.create_index(
        "ix_rfqs_customer_submitted",
        "rfqs",
        ["customer_id", sa.literal_column("submitted_at DESC"), "rfq_id"],
        unique=False,
        schema="business",
        postgresql_using="btree",
    )
    op.create_table(
        "material_inventory",
        sa.Column("inventory_id", sa.Text(), nullable=False),
        sa.Column("material_id", sa.Text(), nullable=False),
        sa.Column("thickness_mm", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("stock_form", sa.Text(), nullable=False),
        sa.Column("quantity_kg", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("reserved_kg", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("warehouse", sa.Text(), nullable=False),
        sa.Column("reorder_threshold_kg", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("supplier_id", sa.Text(), nullable=False),
        sa.Column("supplier_lead_business_days", sa.Integer(), nullable=False),
        sa.Column("expected_replenishment_date", sa.Date(), nullable=True),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column(
            "available_kg",
            sa.Numeric(precision=18, scale=6),
            sa.Computed("quantity_kg - reserved_kg", persisted=True),
            nullable=False,
        ),
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("row_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(
            "available_kg <> 'NaN'::numeric", name=op.f("ck_material_inventory_available_kg_finite")
        ),
        sa.CheckConstraint(
            "quantity_kg <> 'NaN'::numeric", name=op.f("ck_material_inventory_quantity_kg_finite")
        ),
        sa.CheckConstraint(
            "reorder_threshold_kg <> 'NaN'::numeric",
            name=op.f("ck_material_inventory_reorder_threshold_kg_finite"),
        ),
        sa.CheckConstraint(
            "reserved_kg <> 'NaN'::numeric", name=op.f("ck_material_inventory_reserved_kg_finite")
        ),
        sa.CheckConstraint("stock_form = 'sheet'", name=op.f("ck_material_inventory_rule_04")),
        sa.CheckConstraint(
            "thickness_mm <> 'NaN'::numeric", name=op.f("ck_material_inventory_thickness_mm_finite")
        ),
        sa.CheckConstraint("unit = 'kg'", name=op.f("ck_material_inventory_rule_03")),
        sa.CheckConstraint(
            "length(btrim(inventory_id)) > 0 AND length(inventory_id) <= 64",
            name=op.f("ck_material_inventory_inventory_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(material_id)) > 0 AND length(material_id) <= 64",
            name=op.f("ck_material_inventory_material_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(snapshot_id)) > 0 AND length(snapshot_id) <= 64",
            name=op.f("ck_material_inventory_snapshot_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(supplier_id)) > 0 AND length(supplier_id) <= 64",
            name=op.f("ck_material_inventory_supplier_id_identity"),
        ),
        sa.CheckConstraint(
            "quantity_kg >= 0 AND reserved_kg >= 0 AND reserved_kg <= quantity_kg",
            name=op.f("ck_material_inventory_rule_01"),
        ),
        sa.CheckConstraint(
            "reorder_threshold_kg >= 0 AND supplier_lead_business_days >= 0",
            name=op.f("ck_material_inventory_rule_02"),
        ),
        sa.CheckConstraint("row_version >= 1", name=op.f("ck_material_inventory_rule_05")),
        sa.ForeignKeyConstraint(
            ["material_id", "thickness_mm"],
            [
                "business.material_thickness_options.material_id",
                "business.material_thickness_options.thickness_mm",
            ],
            name=op.f("fk_material_inventory_material_id_thickness_mm_material_thickness_options"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["business.data_snapshots.snapshot_id"],
            name=op.f("fk_material_inventory_snapshot_id_data_snapshots"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["business.suppliers.supplier_id"],
            name=op.f("fk_material_inventory_supplier_id_suppliers"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("inventory_id", name=op.f("pk_material_inventory")),
        schema="business",
    )
    op.create_index(
        "ix_inventory_material_thickness",
        "material_inventory",
        ["material_id", "thickness_mm", "warehouse"],
        unique=False,
        schema="business",
        postgresql_using="btree",
    )
    op.create_table(
        "pricing_calculations",
        sa.Column("calculation_id", sa.Text(), nullable=False),
        sa.Column("rfq_id", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requirement_sha256", sa.Text(), nullable=False),
        sa.Column(
            "parts", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "lines", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column("quote_total_usd", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("pricing_rule_version", sa.Text(), nullable=False),
        sa.Column("manufacturing_rule_version", sa.Text(), nullable=False),
        sa.Column(
            "source_snapshot_versions",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(lines) = 'array'", name=op.f("ck_pricing_calculations_lines_shape")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(parts) = 'array'", name=op.f("ck_pricing_calculations_parts_shape")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_snapshot_versions) = 'object'",
            name=op.f("ck_pricing_calculations_source_snapshot_versions_shape"),
        ),
        sa.CheckConstraint(
            "quote_total_usd <> 'NaN'::numeric",
            name=op.f("ck_pricing_calculations_quote_total_usd_finite"),
        ),
        sa.CheckConstraint(
            "requirement_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_pricing_calculations_requirement_sha256_hex"),
        ),
        sa.CheckConstraint(
            "length(btrim(calculation_id)) > 0 AND length(calculation_id) <= 64",
            name=op.f("ck_pricing_calculations_calculation_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(created_by)) > 0 AND length(created_by) <= 64",
            name=op.f("ck_pricing_calculations_created_by_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(manufacturing_rule_version)) > 0",
            name=op.f("ck_pricing_calculations_manufacturing_rule_version_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(pricing_rule_version)) > 0",
            name=op.f("ck_pricing_calculations_pricing_rule_version_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64",
            name=op.f("ck_pricing_calculations_rfq_id_identity"),
        ),
        sa.CheckConstraint(
            "quote_total_usd >= 0 AND expires_at > created_at",
            name=op.f("ck_pricing_calculations_rule_01"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["business.users.user_id"],
            name=op.f("fk_pricing_calculations_created_by_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["manufacturing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            name=op.f("fk_pricing_calculations_manufacturing_rule_version_business_rule_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["pricing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            name=op.f("fk_pricing_calculations_pricing_rule_version_business_rule_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
            name=op.f("fk_pricing_calculations_rfq_id_rfqs"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("calculation_id", name=op.f("pk_pricing_calculations")),
        schema="business",
    )
    op.create_table(
        "quotes",
        sa.Column("quote_id", sa.Text(), nullable=False),
        sa.Column("rfq_id", sa.Text(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_by", sa.Text(), nullable=True),
        sa.Column("approved_version", sa.Integer(), nullable=True),
        sa.Column("approver_id", sa.Text(), nullable=True),
        sa.Column("approval_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("legacy_idempotency_key", sa.Text(), nullable=True),
        sa.Column("row_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("released_by", sa.Text(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status <> 'released' OR released_by IS NOT NULL", name=op.f("ck_quotes_rule_06")
        ),
        sa.CheckConstraint(
            "status IN ('draft','revised_draft','pending_approval','approved','rejected','expired','clarification_required','blocked','released')",
            name=op.f("ck_quotes_rule_07"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved','released') OR (approved_version IS NOT NULL AND approver_id IS NOT NULL AND approval_timestamp IS NOT NULL)",
            name=op.f("ck_quotes_rule_04"),
        ),
        sa.CheckConstraint(
            "(approved_version IS NULL) = (approval_timestamp IS NULL)",
            name=op.f("ck_quotes_rule_08"),
        ),
        sa.CheckConstraint(
            "(released_by IS NULL) = (released_at IS NULL)", name=op.f("ck_quotes_rule_05")
        ),
        sa.CheckConstraint(
            "approved_version IS NULL OR (approver_id IS NOT NULL AND approval_timestamp IS NOT NULL AND approver_id <> created_by)",
            name=op.f("ck_quotes_rule_03"),
        ),
        sa.CheckConstraint(
            "approved_version IS NULL OR approved_version = current_version",
            name=op.f("ck_quotes_rule_02"),
        ),
        sa.CheckConstraint(
            "current_version >= 1 AND row_version >= 1", name=op.f("ck_quotes_rule_01")
        ),
        sa.CheckConstraint(
            "length(btrim(approver_id)) > 0 AND length(approver_id) <= 64",
            name=op.f("ck_quotes_approver_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(created_by)) > 0 AND length(created_by) <= 64",
            name=op.f("ck_quotes_created_by_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(quote_id)) > 0 AND length(quote_id) <= 64",
            name=op.f("ck_quotes_quote_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(released_by)) > 0 AND length(released_by) <= 64",
            name=op.f("ck_quotes_released_by_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64",
            name=op.f("ck_quotes_rfq_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(submitted_by)) > 0 AND length(submitted_by) <= 64",
            name=op.f("ck_quotes_submitted_by_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["approver_id"],
            ["business.users.user_id"],
            name=op.f("fk_quotes_approver_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["business.users.user_id"],
            name=op.f("fk_quotes_created_by_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["released_by"],
            ["business.users.user_id"],
            name=op.f("fk_quotes_released_by_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
            name=op.f("fk_quotes_rfq_id_rfqs"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by"],
            ["business.users.user_id"],
            name=op.f("fk_quotes_submitted_by_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("quote_id", name=op.f("pk_quotes")),
        sa.UniqueConstraint("rfq_id", name=op.f("uq_quotes_rfq_id")),
        schema="business",
    )
    op.create_table(
        "rfq_attachments",
        sa.Column("attachment_id", sa.Text(), nullable=False),
        sa.Column("rfq_id", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("storage_ref", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=True),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("source_role", sa.Text(), nullable=False),
        sa.Column(
            "is_trusted_instruction", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False
        ),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "document_type IN ('pdf','csv','md')", name=op.f("ck_rfq_attachments_rule_01")
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_rfq_attachments_sha256_hex")),
        sa.CheckConstraint(
            "is_trusted_instruction = FALSE", name=op.f("ck_rfq_attachments_rule_02")
        ),
        sa.CheckConstraint(
            "length(btrim(attachment_id)) > 0 AND length(attachment_id) <= 64",
            name=op.f("ck_rfq_attachments_attachment_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64",
            name=op.f("ck_rfq_attachments_rfq_id_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
            name=op.f("fk_rfq_attachments_rfq_id_rfqs"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("attachment_id", name=op.f("pk_rfq_attachments")),
        schema="business",
    )
    op.create_table(
        "rfq_line_items",
        sa.Column("rfq_line_id", sa.UUID(), nullable=False),
        sa.Column("rfq_id", sa.Text(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("part_id", sa.Text(), nullable=False),
        sa.Column("part_type", sa.Text(), nullable=True),
        sa.Column("material_id", sa.Text(), nullable=True),
        sa.Column("finish_id", sa.Text(), nullable=True),
        sa.Column("length_mm", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("width_mm", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("height_mm", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("thickness_mm", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("tolerance_mm", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("bend_radius_mm", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("weld_length_mm", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("hole_count", sa.Integer(), nullable=True),
        sa.Column(
            "source_evidence",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "requirement_revision", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.CheckConstraint(
            "bend_radius_mm <> 'NaN'::numeric", name=op.f("ck_rfq_line_items_bend_radius_mm_finite")
        ),
        sa.CheckConstraint(
            "height_mm <> 'NaN'::numeric", name=op.f("ck_rfq_line_items_height_mm_finite")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_evidence) = 'array'",
            name=op.f("ck_rfq_line_items_source_evidence_shape"),
        ),
        sa.CheckConstraint(
            "length_mm <> 'NaN'::numeric", name=op.f("ck_rfq_line_items_length_mm_finite")
        ),
        sa.CheckConstraint(
            "part_type IN ('flat_panel','mounting_plate','l_bracket','u_bracket','enclosure','fabricated_assembly')",
            name=op.f("ck_rfq_line_items_rule_04"),
        ),
        sa.CheckConstraint(
            "thickness_mm <> 'NaN'::numeric", name=op.f("ck_rfq_line_items_thickness_mm_finite")
        ),
        sa.CheckConstraint(
            "tolerance_mm <> 'NaN'::numeric", name=op.f("ck_rfq_line_items_tolerance_mm_finite")
        ),
        sa.CheckConstraint(
            "weld_length_mm <> 'NaN'::numeric", name=op.f("ck_rfq_line_items_weld_length_mm_finite")
        ),
        sa.CheckConstraint(
            "width_mm <> 'NaN'::numeric", name=op.f("ck_rfq_line_items_width_mm_finite")
        ),
        sa.CheckConstraint(
            "length(btrim(finish_id)) > 0 AND length(finish_id) <= 64",
            name=op.f("ck_rfq_line_items_finish_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(material_id)) > 0 AND length(material_id) <= 64",
            name=op.f("ck_rfq_line_items_material_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(part_id)) > 0 AND length(part_id) <= 64",
            name=op.f("ck_rfq_line_items_part_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64",
            name=op.f("ck_rfq_line_items_rfq_id_identity"),
        ),
        sa.CheckConstraint(
            "length_mm > 0 AND width_mm > 0 AND height_mm > 0 AND thickness_mm > 0 AND bend_radius_mm > 0",
            name=op.f("ck_rfq_line_items_rule_02"),
        ),
        sa.CheckConstraint(
            "line_number > 0 AND requirement_revision > 0", name=op.f("ck_rfq_line_items_rule_01")
        ),
        sa.CheckConstraint(
            "tolerance_mm >= 0 AND weld_length_mm >= 0 AND quantity > 0 AND hole_count >= 0",
            name=op.f("ck_rfq_line_items_rule_03"),
        ),
        sa.ForeignKeyConstraint(
            ["finish_id"],
            ["business.finishes.finish_id"],
            name=op.f("fk_rfq_line_items_finish_id_finishes"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["business.materials.material_id"],
            name=op.f("fk_rfq_line_items_material_id_materials"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
            name=op.f("fk_rfq_line_items_rfq_id_rfqs"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("rfq_line_id", name=op.f("pk_rfq_line_items")),
        sa.UniqueConstraint(
            "rfq_id", "line_number", name=op.f("uq_rfq_line_items_rfq_id_line_number")
        ),
        sa.UniqueConstraint("rfq_id", "part_id", name=op.f("uq_rfq_line_items_rfq_id_part_id")),
        schema="business",
    )
    op.create_table(
        "workflow_runs",
        sa.Column("workflow_run_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("rfq_id", sa.Text(), nullable=True),
        sa.Column("initiated_by", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("external_workflow_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("graph_version", sa.Text(), nullable=False),
        sa.Column(
            "rule_versions",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "source_snapshot_versions",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("checkpoint_ref", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rule_versions) = 'object'",
            name=op.f("ck_workflow_runs_rule_versions_shape"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_snapshot_versions) = 'object'",
            name=op.f("ck_workflow_runs_source_snapshot_versions_shape"),
        ),
        sa.CheckConstraint(
            "status IN ('running','waiting_human','blocked','failed','completed')",
            name=op.f("ck_workflow_runs_rule_01"),
        ),
        sa.CheckConstraint(
            "length(btrim(correlation_id)) > 0",
            name=op.f("ck_workflow_runs_correlation_id_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name=op.f("ck_workflow_runs_customer_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(external_workflow_id)) > 0",
            name=op.f("ck_workflow_runs_external_workflow_id_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(graph_version)) > 0", name=op.f("ck_workflow_runs_graph_version_nonblank")
        ),
        sa.CheckConstraint(
            "length(btrim(initiated_by)) > 0 AND length(initiated_by) <= 64",
            name=op.f("ck_workflow_runs_initiated_by_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64",
            name=op.f("ck_workflow_runs_rfq_id_identity"),
        ),
        sa.CheckConstraint("updated_at >= created_at", name=op.f("ck_workflow_runs_rule_02")),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["business.customers.customer_id"],
            name=op.f("fk_workflow_runs_customer_id_customers"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["initiated_by"],
            ["business.users.user_id"],
            name=op.f("fk_workflow_runs_initiated_by_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
            name=op.f("fk_workflow_runs_rfq_id_rfqs"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("workflow_run_id", name=op.f("pk_workflow_runs")),
        sa.UniqueConstraint("correlation_id", name=op.f("uq_workflow_runs_correlation_id")),
        schema="orchestration",
    )
    op.create_index(
        "ix_workflow_runs_rfq_time",
        "workflow_runs",
        ["rfq_id", "created_at", "workflow_run_id"],
        unique=False,
        schema="orchestration",
        postgresql_using="btree",
        postgresql_where=sa.text("rfq_id IS NOT NULL"),
    )
    op.create_table(
        "procurement_records",
        sa.Column("procurement_id", sa.Text(), nullable=False),
        sa.Column("inventory_id", sa.Text(), nullable=False),
        sa.Column("supplier_id", sa.Text(), nullable=False),
        sa.Column("quantity_kg", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("ordered_date", sa.Date(), nullable=False),
        sa.Column("expected_receipt_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "quantity_kg <> 'NaN'::numeric", name=op.f("ck_procurement_records_quantity_kg_finite")
        ),
        sa.CheckConstraint(
            "status IN ('ordered','delayed')", name=op.f("ck_procurement_records_rule_02")
        ),
        sa.CheckConstraint(
            "length(btrim(inventory_id)) > 0 AND length(inventory_id) <= 64",
            name=op.f("ck_procurement_records_inventory_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(procurement_id)) > 0 AND length(procurement_id) <= 64",
            name=op.f("ck_procurement_records_procurement_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(supplier_id)) > 0 AND length(supplier_id) <= 64",
            name=op.f("ck_procurement_records_supplier_id_identity"),
        ),
        sa.CheckConstraint(
            "quantity_kg > 0 AND expected_receipt_date >= ordered_date",
            name=op.f("ck_procurement_records_rule_01"),
        ),
        sa.ForeignKeyConstraint(
            ["inventory_id"],
            ["business.material_inventory.inventory_id"],
            name=op.f("fk_procurement_records_inventory_id_material_inventory"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["business.suppliers.supplier_id"],
            name=op.f("fk_procurement_records_supplier_id_suppliers"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("procurement_id", name=op.f("pk_procurement_records")),
        schema="business",
    )
    op.create_index(
        "ix_procurement_inventory_status",
        "procurement_records",
        ["inventory_id", "status"],
        unique=False,
        schema="business",
        postgresql_using="btree",
    )
    op.create_index(
        "ix_procurement_supplier_status_eta",
        "procurement_records",
        ["supplier_id", "status", "expected_receipt_date"],
        unique=False,
        schema="business",
        postgresql_using="btree",
    )
    op.create_table(
        "quote_versions",
        sa.Column("quote_version_id", sa.Text(), nullable=False),
        sa.Column("quote_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("approval_valid", sa.Boolean(), nullable=False),
        sa.Column("currency", sa.Text(), server_default=sa.text("'USD'"), nullable=False),
        sa.Column("total_usd", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("pricing_rule_version", sa.Text(), nullable=True),
        sa.Column("manufacturing_rule_version", sa.Text(), nullable=True),
        sa.Column("approval_rule_version", sa.Text(), nullable=True),
        sa.Column("workflow_rule_version", sa.Text(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("estimated_lead_business_days", sa.Integer(), nullable=True),
        sa.Column(
            "source_snapshot_versions",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("calculation_id", sa.Text(), nullable=True),
        sa.Column("snapshot_completeness", sa.Text(), nullable=False),
        sa.CheckConstraint("currency = 'USD'", name=op.f("ck_quote_versions_rule_02")),
        sa.CheckConstraint(
            "jsonb_typeof(source_snapshot_versions) = 'object'",
            name=op.f("ck_quote_versions_source_snapshot_versions_shape"),
        ),
        sa.CheckConstraint(
            "snapshot_completeness <> 'complete' OR (manufacturing_rule_version IS NOT NULL AND approval_rule_version IS NOT NULL AND workflow_rule_version IS NOT NULL AND source_snapshot_versions IS NOT NULL AND calculation_id IS NOT NULL AND expiry_date IS NOT NULL AND estimated_lead_business_days IS NOT NULL)",
            name=op.f("ck_quote_versions_rule_07"),
        ),
        sa.CheckConstraint(
            "snapshot_completeness <> 'metadata_only' OR total_usd IS NULL",
            name=op.f("ck_quote_versions_rule_05"),
        ),
        sa.CheckConstraint(
            "snapshot_completeness = 'metadata_only' OR (total_usd IS NOT NULL AND pricing_rule_version IS NOT NULL)",
            name=op.f("ck_quote_versions_rule_06"),
        ),
        sa.CheckConstraint(
            "snapshot_completeness IN ('metadata_only','priced_partial','complete')",
            name=op.f("ck_quote_versions_rule_04"),
        ),
        sa.CheckConstraint(
            "total_usd <> 'NaN'::numeric", name=op.f("ck_quote_versions_total_usd_finite")
        ),
        sa.CheckConstraint(
            "NOT approval_valid OR is_current", name=op.f("ck_quote_versions_rule_08")
        ),
        sa.CheckConstraint(
            "length(btrim(approval_rule_version)) > 0",
            name=op.f("ck_quote_versions_approval_rule_version_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(calculation_id)) > 0 AND length(calculation_id) <= 64",
            name=op.f("ck_quote_versions_calculation_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(created_by)) > 0 AND length(created_by) <= 64",
            name=op.f("ck_quote_versions_created_by_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(manufacturing_rule_version)) > 0",
            name=op.f("ck_quote_versions_manufacturing_rule_version_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(pricing_rule_version)) > 0",
            name=op.f("ck_quote_versions_pricing_rule_version_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(quote_id)) > 0 AND length(quote_id) <= 64",
            name=op.f("ck_quote_versions_quote_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(quote_version_id)) > 0 AND length(quote_version_id) <= 64",
            name=op.f("ck_quote_versions_quote_version_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(workflow_rule_version)) > 0",
            name=op.f("ck_quote_versions_workflow_rule_version_nonblank"),
        ),
        sa.CheckConstraint(
            "total_usd >= 0 AND estimated_lead_business_days >= 0",
            name=op.f("ck_quote_versions_rule_03"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_quote_versions_rule_01")),
        sa.ForeignKeyConstraint(
            ["approval_rule_version"],
            ["business.business_rule_versions.rule_version"],
            name=op.f("fk_quote_versions_approval_rule_version_business_rule_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["calculation_id"],
            ["business.pricing_calculations.calculation_id"],
            name=op.f("fk_quote_versions_calculation_id_pricing_calculations"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["business.users.user_id"],
            name=op.f("fk_quote_versions_created_by_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["manufacturing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            name=op.f("fk_quote_versions_manufacturing_rule_version_business_rule_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["pricing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            name=op.f("fk_quote_versions_pricing_rule_version_business_rule_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"],
            ["business.quotes.quote_id"],
            name=op.f("fk_quote_versions_quote_id_quotes"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_rule_version"],
            ["business.business_rule_versions.rule_version"],
            name=op.f("fk_quote_versions_workflow_rule_version_business_rule_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("quote_version_id", name=op.f("pk_quote_versions")),
        sa.UniqueConstraint("quote_id", "version", name=op.f("uq_quote_versions_quote_id_version")),
        schema="business",
    )
    op.create_index(
        "uq_quote_versions_current",
        "quote_versions",
        ["quote_id"],
        unique=True,
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "approval_requests",
        sa.Column("approval_request_id", sa.Text(), nullable=False),
        sa.Column("quote_id", sa.Text(), nullable=False),
        sa.Column("quote_version", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "required_role", sa.Text(), server_default=sa.text("'sales_manager'"), nullable=False
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("approval_rule_version", sa.Text(), nullable=False),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'pending' AND resolved_by IS NULL AND resolved_at IS NULL) OR (status <> 'pending' AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name=op.f("ck_approval_requests_rule_03"),
        ),
        sa.CheckConstraint(
            "required_role = 'sales_manager'", name=op.f("ck_approval_requests_rule_02")
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','superseded')",
            name=op.f("ck_approval_requests_rule_01"),
        ),
        sa.CheckConstraint(
            "length(btrim(approval_request_id)) > 0 AND length(approval_request_id) <= 64",
            name=op.f("ck_approval_requests_approval_request_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(approval_rule_version)) > 0",
            name=op.f("ck_approval_requests_approval_rule_version_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(quote_id)) > 0 AND length(quote_id) <= 64",
            name=op.f("ck_approval_requests_quote_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(requested_by)) > 0 AND length(requested_by) <= 64",
            name=op.f("ck_approval_requests_requested_by_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(resolved_by)) > 0 AND length(resolved_by) <= 64",
            name=op.f("ck_approval_requests_resolved_by_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["approval_rule_version"],
            ["business.business_rule_versions.rule_version"],
            name=op.f("fk_approval_requests_approval_rule_version_business_rule_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["quote_id", "quote_version"],
            ["business.quote_versions.quote_id", "business.quote_versions.version"],
            name=op.f("fk_approval_requests_quote_id_quote_version_quote_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["business.users.user_id"],
            name=op.f("fk_approval_requests_requested_by_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["required_role"],
            ["business.roles.role"],
            name=op.f("fk_approval_requests_required_role_roles"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["business.users.user_id"],
            name=op.f("fk_approval_requests_resolved_by_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("approval_request_id", name=op.f("pk_approval_requests")),
        sa.UniqueConstraint(
            "approval_request_id",
            "quote_id",
            "quote_version",
            name=op.f("uq_approval_requests_approval_request_id_quote_id_quote_version"),
        ),
        schema="business",
    )
    op.create_index(
        "ix_approval_requests_queue",
        "approval_requests",
        ["required_role", "requested_at", "quote_id", "quote_version"],
        unique=False,
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_approval_requests_pending",
        "approval_requests",
        ["quote_id", "quote_version"],
        unique=True,
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("agent_name", sa.Text(), nullable=True),
        sa.Column("customer_id", sa.Text(), nullable=True),
        sa.Column("rfq_id", sa.Text(), nullable=True),
        sa.Column("quote_id", sa.Text(), nullable=True),
        sa.Column("quote_version", sa.Integer(), nullable=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column(
            "event_data",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(event_data) = 'object'", name=op.f("ck_audit_events_event_data_shape")
        ),
        sa.CheckConstraint(
            "outcome IN ('success','denied','error')", name=op.f("ck_audit_events_rule_01")
        ),
        sa.CheckConstraint(
            "length(btrim(actor_id)) > 0 AND length(actor_id) <= 64",
            name=op.f("ck_audit_events_actor_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(correlation_id)) > 0",
            name=op.f("ck_audit_events_correlation_id_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name=op.f("ck_audit_events_customer_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(quote_id)) > 0 AND length(quote_id) <= 64",
            name=op.f("ck_audit_events_quote_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64",
            name=op.f("ck_audit_events_rfq_id_identity"),
        ),
        sa.CheckConstraint(
            "quote_version IS NULL OR (quote_id IS NOT NULL AND quote_version >= 1)",
            name=op.f("ck_audit_events_rule_02"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["business.users.user_id"],
            name=op.f("fk_audit_events_actor_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["business.customers.customer_id"],
            name=op.f("fk_audit_events_customer_id_customers"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["quote_id", "quote_version"],
            ["business.quote_versions.quote_id", "business.quote_versions.version"],
            name=op.f("fk_audit_events_quote_id_quote_version_quote_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"],
            ["business.quotes.quote_id"],
            name=op.f("fk_audit_events_quote_id_quotes"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
            name=op.f("fk_audit_events_rfq_id_rfqs"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_audit_events")),
        schema="business",
    )
    op.create_index(
        "ix_audit_correlation_time",
        "audit_events",
        ["correlation_id", "occurred_at", "event_id"],
        unique=False,
        schema="business",
        postgresql_using="btree",
    )
    op.create_index(
        "ix_audit_rfq_time",
        "audit_events",
        ["rfq_id", "occurred_at", "event_id"],
        unique=False,
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("rfq_id IS NOT NULL"),
    )
    op.create_table(
        "quote_line_items",
        sa.Column("quote_line_id", sa.Text(), nullable=False),
        sa.Column("quote_id", sa.Text(), nullable=False),
        sa.Column("quote_version", sa.Integer(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("part_id", sa.Text(), nullable=False),
        sa.Column(
            "part_json", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "cost_breakdown_json",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("line_total_usd", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(part_json) = 'object' AND jsonb_typeof(cost_breakdown_json) = 'object'",
            name=op.f("ck_quote_line_items_rule_02"),
        ),
        sa.CheckConstraint(
            "line_total_usd <> 'NaN'::numeric",
            name=op.f("ck_quote_line_items_line_total_usd_finite"),
        ),
        sa.CheckConstraint(
            "length(btrim(part_id)) > 0 AND length(part_id) <= 64",
            name=op.f("ck_quote_line_items_part_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(quote_id)) > 0 AND length(quote_id) <= 64",
            name=op.f("ck_quote_line_items_quote_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(quote_line_id)) > 0 AND length(quote_line_id) <= 64",
            name=op.f("ck_quote_line_items_quote_line_id_identity"),
        ),
        sa.CheckConstraint(
            "quote_version >= 1 AND line_number > 0 AND quantity > 0 AND line_total_usd >= 0",
            name=op.f("ck_quote_line_items_rule_01"),
        ),
        sa.ForeignKeyConstraint(
            ["quote_id", "quote_version"],
            ["business.quote_versions.quote_id", "business.quote_versions.version"],
            name=op.f("fk_quote_line_items_quote_id_quote_version_quote_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("quote_line_id", name=op.f("pk_quote_line_items")),
        sa.UniqueConstraint(
            "quote_id",
            "quote_version",
            "line_number",
            name=op.f("uq_quote_line_items_quote_id_quote_version_line_number"),
        ),
        schema="business",
    )
    op.create_table(
        "approval_history",
        sa.Column("approval_id", sa.Text(), nullable=False),
        sa.Column("quote_id", sa.Text(), nullable=False),
        sa.Column("quote_version", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("action_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approval_request_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "action IN ('submitted','approved','rejected','released','superseded')",
            name=op.f("ck_approval_history_rule_01"),
        ),
        sa.CheckConstraint(
            "length(btrim(actor_id)) > 0 AND length(actor_id) <= 64",
            name=op.f("ck_approval_history_actor_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(approval_id)) > 0 AND length(approval_id) <= 64",
            name=op.f("ck_approval_history_approval_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(approval_request_id)) > 0 AND length(approval_request_id) <= 64",
            name=op.f("ck_approval_history_approval_request_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(quote_id)) > 0 AND length(quote_id) <= 64",
            name=op.f("ck_approval_history_quote_id_identity"),
        ),
        sa.CheckConstraint("quote_version >= 1", name=op.f("ck_approval_history_rule_02")),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["business.users.user_id"],
            name=op.f("fk_approval_history_actor_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["approval_request_id", "quote_id", "quote_version"],
            [
                "business.approval_requests.approval_request_id",
                "business.approval_requests.quote_id",
                "business.approval_requests.quote_version",
            ],
            name=op.f(
                "fk_approval_history_approval_request_id_quote_id_quote_version_approval_requests"
            ),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["quote_id", "quote_version"],
            ["business.quote_versions.quote_id", "business.quote_versions.version"],
            name=op.f("fk_approval_history_quote_id_quote_version_quote_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("approval_id", name=op.f("pk_approval_history")),
        schema="business",
    )
    op.create_index(
        "ix_approval_history_version_time",
        "approval_history",
        ["quote_id", "quote_version", "action_at", "approval_id"],
        unique=False,
        schema="business",
        postgresql_using="btree",
    )
    # Quote pointers close a dependency cycle only after quote_versions exists.
    for column in ("current_version", "approved_version"):
        op.create_foreign_key(
            op.f(f"fk_quotes_quote_id_{column}_quote_versions"),
            "quotes",
            "quote_versions",
            ["quote_id", column],
            ["quote_id", "version"],
            source_schema="business",
            referent_schema="business",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
            deferrable=True,
            initially="DEFERRED",
        )


def downgrade() -> None:
    """Downgrade schema."""
    # Frozen object ownership shape for this revision; never derived from future models.
    ensure_test_downgrade(
        op.get_bind(), revision, "8ef8e5a94fd625915001aa834f4a37cddf20abc122a087f934d227b7424da9ff"
    )
    for column in ("current_version", "approved_version"):
        op.drop_constraint(
            op.f(f"fk_quotes_quote_id_{column}_quote_versions"),
            "quotes",
            schema="business",
            type_="foreignkey",
        )
    op.drop_index(
        "ix_approval_history_version_time",
        table_name="approval_history",
        schema="business",
        postgresql_using="btree",
    )
    op.drop_table("approval_history", schema="business")
    op.drop_table("quote_line_items", schema="business")
    op.drop_index(
        "ix_audit_rfq_time",
        table_name="audit_events",
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("rfq_id IS NOT NULL"),
    )
    op.drop_index(
        "ix_audit_correlation_time",
        table_name="audit_events",
        schema="business",
        postgresql_using="btree",
    )
    op.drop_table("audit_events", schema="business")
    op.drop_index(
        "uq_approval_requests_pending",
        table_name="approval_requests",
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(
        "ix_approval_requests_queue",
        table_name="approval_requests",
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("approval_requests", schema="business")
    op.drop_index(
        "uq_quote_versions_current",
        table_name="quote_versions",
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("is_current"),
    )
    op.drop_table("quote_versions", schema="business")
    op.drop_index(
        "ix_procurement_supplier_status_eta",
        table_name="procurement_records",
        schema="business",
        postgresql_using="btree",
    )
    op.drop_index(
        "ix_procurement_inventory_status",
        table_name="procurement_records",
        schema="business",
        postgresql_using="btree",
    )
    op.drop_table("procurement_records", schema="business")
    op.drop_index(
        "ix_workflow_runs_rfq_time",
        table_name="workflow_runs",
        schema="orchestration",
        postgresql_using="btree",
        postgresql_where=sa.text("rfq_id IS NOT NULL"),
    )
    op.drop_table("workflow_runs", schema="orchestration")
    op.drop_table("rfq_line_items", schema="business")
    op.drop_table("rfq_attachments", schema="business")
    op.drop_table("quotes", schema="business")
    op.drop_table("pricing_calculations", schema="business")
    op.drop_index(
        "ix_inventory_material_thickness",
        table_name="material_inventory",
        schema="business",
        postgresql_using="btree",
    )
    op.drop_table("material_inventory", schema="business")
    op.drop_index(
        "ix_rfqs_customer_submitted", table_name="rfqs", schema="business", postgresql_using="btree"
    )
    op.drop_table("rfqs", schema="business")
    op.drop_table("production_capacity", schema="business")
    op.drop_table("operation_routing", schema="business")
    op.drop_table("material_thickness_options", schema="business")
    op.drop_table("material_supported_operations", schema="business")
    op.drop_table("material_finish_compatibility", schema="business")
    op.drop_table("idempotency_records", schema="business")
    op.drop_table("finish_material_categories", schema="business")
    op.drop_table("workcentres", schema="business")
    op.drop_index(
        "ix_users_reviewers",
        table_name="users",
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("active"),
    )
    op.drop_table("users", schema="business")
    op.drop_table("role_permissions", schema="business")
    op.drop_table("materials", schema="business")
    op.drop_table("finishes", schema="business")
    op.drop_index(
        "uq_contacts_primary",
        table_name="contacts",
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("is_primary"),
    )
    op.drop_index(
        "ix_contacts_customer_name",
        table_name="contacts",
        schema="business",
        postgresql_using="btree",
    )
    op.drop_table("contacts", schema="business")
    op.drop_table("suppliers", schema="business")
    op.drop_table("roles", schema="business")
    op.drop_table("permissions", schema="business")
    op.drop_table("operations", schema="business")
    op.drop_table("data_snapshots", schema="business")
    op.drop_table("customers", schema="business")
    op.drop_table("business_rule_versions", schema="business")
    op.execute(sa.schema.DropSchema("orchestration", cascade=False))
    op.execute(sa.schema.DropSchema("business", cascade=False))
