{#-
  Where the touchstone verdicts projection lives. `touchstone publish` writes it
  wholesale after each scan; these macros only ever read it.

  Override per project when the projection lives elsewhere:

      vars:
        touchstone_database: analytics      # default: target.database
        touchstone_schema: touchstone
        touchstone_table: verdicts
-#}

{% macro findings() %}
    {#- Built as a Relation so the adapter quotes it: an unquoted
        `my-proj.touchstone.verdicts` was a syntax error on BigQuery, and a
        None database on dbt-databricks rendered `None.touchstone.verdicts`. -#}
    {%- set rel = api.Relation.create(
            database=var('touchstone_database', target.database),
            schema=var('touchstone_schema', 'touchstone'),
            identifier=var('touchstone_table', 'verdicts')) -%}
    {{ rel }}
{% endmacro %}


{#-
  Tolerant relation match: the projection stores relations as the scan saw
  them (unquoted, possibly two-part), while the model renders fully
  qualified. Compare case-insensitively, and let a two-part stored relation
  match the model's schema.identifier suffix.

  With a `where:` test config dbt passes `model` as a SUBQUERY STRING, so
  the database/schema/identifier attributes render empty and every relation
  compared against '..' — the test passed on the affected model. That
  configuration is refused loudly instead.
-#}
{% macro matches_relation(column, model) %}
    {%- if model is string -%}
        {{ exceptions.raise_compiler_error(
            "touchstone tests do not support the `where` config: it replaces the "
            "model relation with a subquery, and the relation match would "
            "silently pass") }}
    {%- endif -%}
    (
        lower({{ column }}) = lower('{{ model.database }}.{{ model.schema }}.{{ model.identifier }}')
        or lower({{ column }}) = lower('{{ model.schema }}.{{ model.identifier }}')
    )
{% endmacro %}
