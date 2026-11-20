# ysda-crowdsourcing-final-project

## Оценка моделей

```shell
uv run materialize -i path/to/golden_set.json
uv run evaluate -i path/to/golden_set.json -m model
```

## Предсказание

```shell
uv run materialize-dataset -i data/dataset.jsonl -o data/images/prediction/
uv run predict -m qwen --images data/images/prediction/ -o data/qwen_prediction.jsonl
uv run recover-predictions -i data/qwen_prediction.attempts.jsonl -o data/qwen_prediction.jsonl
uv run render-predictions -i data/qwen_prediction.jsonl -o data/images/qwen_results
```
