# Open questions

Technical and business questions the prototype could not answer. The generic example task (image
classification with a small public model on synthetic images) stands in until they are answered.

## Business requirements

1. **Image types and analysis task.** Which image formats, sizes, bit depths and modalities; which task
   (classification, detection, segmentation, quality inspection, text recognition, ...)? This decides the model,
   the normaliser (`normalize_max_side`, colour handling) and whether CPU inference is adequate.
2. **Volume and variability.** Typical and peak arrivals, burst size, length of idle periods, daily/weekly
   pattern. These drive `max_*`, the scaling steps and whether `min_*` should be above zero.
3. **Latency requirement.** Required end-to-end time (upload -> searchable) per class of work. If tighter than the
   elastic cold start, a capacity floor or a real-time endpoint is needed (docs/architecture.md, FR-3 table).
4. **Retention.** How long originals, normalised images, inference outputs and search documents must be kept or
   must be deleted (lifecycle rules are parameters).
5. **Metadata model.** Which metadata fields must be searchable (currently: category, tags, type, size, dimensions,
   upload time, status, top classes and score).
6. **Callers.** Who calls the API (services, people, other accounts)? IAM-only access is assumed; an identity
   provider or cross-account access changes the resource policy and the threat model.

## Technical questions

7. **Training-data terms of the example weights.** The weights are Apache-2.0 on the model card; they were trained on
   ImageNet-1k, whose access terms are separate. Confirm with your legal team before using the example model beyond a
   demo, or replace it with a model trained on data you may use.
8. **GPU need.** Measure the target model with `scripts/loadtest.py` on CPU first; move to GPU only if the
   measured per-instance throughput or end-to-end time does not meet item 3.
9. **Account-level prerequisites.** The deployment assumes the API Gateway account CloudWatch Logs role and the
   service-linked roles already exist (preflight checks, does not create them). The account owner decides if missing.
10. **Lost notifications.** Should a sweeper re-check jobs stuck in `INVOKED` (notification failures) against
    `async-output/`? Not implemented in the prototype.
11. **Code owner.** CODEOWNERS names `@shaojiez202608`; confirm the maintainer handle before publishing.
