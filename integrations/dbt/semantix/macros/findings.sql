{#-
  Where the semantix verdicts projection lives. `semantix publish` writes it
  wholesale after each scan; these macros only ever read it.

  Override per project when the projection lives elsewhere:

      vars:
        semantix_database: analytics      # default: target.database
        semantix_schema: semantix
        semantix_table: verdicts
-#}

{% macro findings() %}
    {#- Built as a Relation so the adapter quotes it: an unquoted
        `my-proj.semantix.verdicts` was a syntax error on BigQuery, and a
        None database on dbt-databricks rendered `None.semantix.verdicts`. -#}
    {%- set rel = api.Relation.create(
            database=var('semantix_database', target.database),
            schema=var('semantix_schema', 'semantix'),
            identifier=var('semantix_table', 'verdicts')) -%}
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
            "semantix tests do not support the `where` config: it replaces the "
            "model relation with a subquery, and the relation match would "
            "silently pass") }}
    {%- endif -%}
    (
        lower({{ column }}) = lower('{{ model.database }}.{{ model.schema }}.{{ model.identifier }}')
        or lower({{ column }}) = lower('{{ model.schema }}.{{ model.identifier }}')
    )
{% endmacro %}
