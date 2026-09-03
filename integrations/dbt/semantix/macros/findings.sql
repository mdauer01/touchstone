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
    {{ var('semantix_database', target.database) }}.{{ var('semantix_schema', 'semantix') }}.{{ var('semantix_table', 'verdicts') }}
{% endmacro %}


{#-
  Tolerant relation match: the projection stores relations as the scan saw
  them (unquoted, possibly two-part), while `{{ this }}` renders fully
  qualified. Compare case-insensitively, and let a two-part stored relation
  match the model's schema.identifier suffix.
-#}
{% macro matches_relation(column, model) %}
    (
        lower({{ column }}) = lower('{{ model.database }}.{{ model.schema }}.{{ model.identifier }}')
        or lower({{ column }}) = lower('{{ model.schema }}.{{ model.identifier }}')
    )
{% endmacro %}
