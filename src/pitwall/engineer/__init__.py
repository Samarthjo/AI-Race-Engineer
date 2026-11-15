"""The race engineer (Stage 5): reports and answers built only from measured facts.

* :mod:`facts`: facts with stable ids; the only material the AI may use.
* :mod:`report`: the deterministic report (5a). It is the ground truth and is useful alone.
* :mod:`llm`: the language-model provider interface and the local Ollama provider.
* :mod:`validate`: the grounding check: every number, corner and claim must trace to facts.
* :mod:`narrator`: the AI write-up of a report (5b), checked, retried once, else the
  deterministic report.
* :mod:`qa`: questions and answers with read-only tools over the analysis (5c).
* :mod:`store`: stored AI texts (L5) with provider, model, prompt version and check results.
* :mod:`service`: building everything for a session, lap and reference.

Raw data, calculated metrics and AI-generated text stay separate: the AI never sees
telemetry, only facts, and its text is stored apart from the analysis.
"""
