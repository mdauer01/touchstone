{#-
  Generic tests over the semantix verdicts projection. A dbt test is "query
  returns rows -> fail", and a standing finding keeps returning its row until
  it is resolved — state, not a one-shot alert. Severity, muting and routing
  are dbt's own (`severity: warn|error`), so gating on semantic findings
  inherits the triage muscle the project already has.
-#}

{#- Fails while any CONTRADICTION touches this model's relation (or the
    named concept). The strongest gate: same name, different math. -#}
{% test no_contradictions(model, concept=none) %}
    select concept, verdict, severity, summary, relation
    from {{ semantix.findings() }}
    where verdict = 'CONTRADICTION'
      and (
          {{ semantix.matches_relation('relation', model) }}
          {% if concept %} or lower(concept) = lower('{{ concept }}') {% endif %}
      )
{% endtest %}


{#- Fails unless the named concept is affirmatively CERTIFIED — a missing
    concept, a contradiction, or an unresolved state all fail. Attach to the
    model that owns the metric. -#}
{% test certified(model, concept) %}
    with concept_verdicts as (
        select verdict, severity, summary
        from {{ semantix.findings() }}
        where lower(concept) = lower('{{ concept }}')
    )
    select 'CONTRADICTION stands' as reason, severity, summary
    from concept_verdicts where verdict = 'CONTRADICTION'
    union all
    select 'no CERTIFIED verdict for {{ concept }}' as reason, 0, null
    where not exists (select 1 from concept_verdicts where verdict = 'CERTIFIED')
{% endtest %}


{#- Fails when the projection itself is stale: verdicts describe a scan, and
    a scan describes a moment. Gating on old verdicts is gating on nothing —
    this test says so inside the same rail. Attach to any model. -#}
{% test fresh_scan(model, max_hours=24) %}
    select scan_id, scanned_at
    from {{ semantix.findings() }}
    where scanned_at < {{ dbt.dateadd('hour', -max_hours, 'current_timestamp') }}
    limit 1
{% endtest %}
