{#-
  Generic tests over the touchstone verdicts projection. A dbt test is "query
  returns rows -> fail", and a standing finding keeps returning its row until
  it is resolved — state, not a one-shot alert. Severity, muting and routing
  are dbt's own (`severity: warn|error`), so gating on semantic findings
  inherits the triage muscle the project already has.
-#}

{#- Fails while any SUSPECTED_DIVERGENCE (or a legacy CONTRADICTION row)
    touches this model's relation or the named concept: same name, apparently
    different math. A triage gate on a suspicion, not a verified finding. -#}
{% test no_contradictions(model, concept=none) %}
    select concept, verdict, severity, summary, relation
    from {{ touchstone.findings() }}
    where verdict in ('SUSPECTED_DIVERGENCE', 'CONTRADICTION')
      and (
          {{ touchstone.matches_relation('relation', model) }}
          {% if concept %} or lower(concept) = lower('{{ concept }}') {% endif %}
      )
{% endtest %}


{#- Fails closed: positive verdicts are suspended (touchstone.conclusions), so
    no concept is certified — whatever the projection holds, including rows
    published before the suspension. Kept so existing attachments fail loudly
    with the reason instead of passing. -#}
{% test certified(model, concept) %}
    select 'positive verdicts are suspended — {{ concept }} is not certified' as reason,
           cast(0 as integer) as severity, cast(null as {{ dbt.type_string() }}) as summary
    from (select 1 as one) as anchor
{% endtest %}


{#- Fails when the projection itself is stale: verdicts describe a scan, and
    a scan describes a moment. Gating on old verdicts is gating on nothing —
    this test says so inside the same rail. Attach to any model. -#}
{% test fresh_scan(model, max_hours=24) %}
    select scan_id, scanned_at
    from {{ touchstone.findings() }}
    {#- Cast the cutoff: on BigQuery dateadd yields DATETIME and comparing it
        with a TIMESTAMP column is a type error. -#}
    where scanned_at < cast({{ dbt.dateadd('hour', -max_hours, 'current_timestamp') }}
                            as {{ dbt.type_timestamp() }})
    limit 1
{% endtest %}
