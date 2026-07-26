# Slice 010.1 Acceptance Erratum

- Original acceptance file: `tests/acceptance/test_slice0101_ceo_nervous_system.py`
- Original SHA-256: `D0F92F6C86AF33B335249649E2C3522D991A6C517916069C06B40F1BC4517DF6`
- Recorded: 2026-07-26 Asia/Taipei
- Reason: Slice 010.2 requires transparent correction of a logically impossible
  case-sensitive literal after the frozen Slice 010.1 run exposed the defect.

## Defective assertion

```python
assert "maximum HR assessment authorization" in page.lower()
```

`page.lower()` converts every ASCII `H` and `R` in the rendered page to
lowercase. Therefore an uppercase ASCII substring `HR` can never occur in the
lowercased value, independent of application behavior.

## Corrected assertion

```python
assert "maximum hr assessment authorization" in page.lower()
```

Only the literal was corrected. The route, required visible wording, absence
of the manual assessment form, and every other acceptance assertion remain
unchanged. This does not weaken product behavior; it makes the intended
case-insensitive check executable.

- Corrected SHA-256: `AD5610D0637B6E7450E807BF97547339DB3133A8BD367044B10115D68B48E2ED`
