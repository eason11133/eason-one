# Slice 010.2 Pre-implementation Fixture Erratum

The first acceptance baseline was run before any Slice 010.2 production-code
change. It exposed the intended missing production behavior and one independent
test-fixture error.

- Initial test SHA-256:
  `22E92BB6F4082F416C93DDEA5FDE1AEA698B691824089AD102CF65C8AD364808`
- Defect: the transcript-exclusion sentinel attempted to insert a
  `MeetingMessage` with `meeting_id=None`, although the existing audited
  Meeting model correctly requires every message to belong to a Meeting.
- Correction: create a real unrelated audit Meeting and attach the sentinel
  transcript to it.
- Product assertions changed: none.
- Production code changed before correction: none.

The corrected acceptance file is re-frozen before Slice 010.2 implementation.

- Corrected frozen SHA-256:
  `7C743A9F41CAF3ACB63E91010EBFF796217910C43CE23E66B495FBA686F94D86`
