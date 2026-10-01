# synthetic_v1 (SYNTHETIC evaluation dataset)

* `questions.jsonl`: retrieval/answer items. `expected_titles` are the documents a correct answer must cite;
  `expect_abstain` marks unanswerable or unauthorized items.
* `scenarios.jsonl`: scripted lifecycle/failure scenarios executed by the runner.
* `extra_documents`: synthetic binary fixtures (PDF with table, scanned PDF, DOCX) are generated at run time
  by `erp_evals.fixtures` and ingested together with `data/synthetic`.

Labels were written by the developer for pipeline testing. They are **not** human-reviewed relevance judgements and
must not be used to claim model quality.
