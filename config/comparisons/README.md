# Paired forecasts

`comparisons/*.json` owns the paired experiment's product IDs, fixed probes and
consumer layer selection. Both products use the same forcing cycle and next-day
horizon. The control geometry equals the fine run's outer domain; fixed clocks
isolate resolution from adaptive-timestep policy. Run both through the ordinary
`geog`, `plan`, `prep`, `acquire`, `solve`, `post`, and `publish` commands.
Use separate scratch directories and geography bundles for each product.

Export the consumer registrations from these profiles:

```sh
uv run python -m scripts.export_comparison --campaign CONFIG_CAMPAIGN_JSON \
  --registry CONSUMER_MODELS_JSON --out NEW_MODELS_JSON
```

Apply the generated registry in the consumer, copy the campaign JSON, regenerate
coverage masks and reference docs, and stage terrain through its normal workflow.
The consumer's `comparison wrf-plan`, `wrf-capture`, and `wrf-report` commands
archive pinned builds and compare them with the baseline and optional observations.
A full-horizon native solver run and target-host timing qualify delivery; the
configuration and synthetic checks establish geometry and handoff correctness.
