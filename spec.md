## Problem

When a game-log request fails validation, the client is told only that something was wrong, never what. `_parse_game_log_filters` raises `InvalidInputError("One or more game log filters are invalid.", detail=error)`, and `detail` is sanitised and written to the log — `public_details` stays `None`, so nothing identifying the offending parameter reaches the caller.

That is fine when the frontend can vet a value itself. It is not fine when only this service knows the answer. The clearest case is `teams_against`: the model already builds a precise message naming the unsupported entries and listing every supported filter, and the caller sees none of it.

## Why it matters now

The frontend has just made Filter Sets addressable — a URL carries the game-log query string, and a link can be bookmarked and shared. Its rule is that a parameter it cannot honour names itself and withholds the whole Filter Set, so the user is never shown results that disagree with the link they followed.

For most parameters it can hold up its end. For `teams_against` it cannot, without keeping a copy of `SUPPORTED_TEAM_FILTERS` plus `TEAM_FILTER_ALIASES` in the frontend — a duplicate that would drift from this service and start refusing links this service would honour. That already happened once: the frontend was validating against its own dropdown, which is narrower than the supported set, and refused valid links carrying `Arc3Assists` and similar. The fix was to stop validating names there and let this service decide.

The consequence is a gap. A bogus opponent filter now produces a generic error naming nothing, when this service knew exactly which value was wrong and had already composed the sentence.

## Outcome

Return the identifying facts for a rejected game-log filter through `public_details`, so a caller can name the offending parameter and value without holding a copy of this service's vocabulary. `AppError.public_details` already exists for this purpose.

Scope to what a caller can act on — which parameter failed and which submitted values were unusable. Do not return internal diagnostics, provider responses, or anything the sanitiser exists to strip.

## Done when

- [ ] A rejected game-log filter returns, in the error payload, the parameter that failed and the offending values.
- [ ] `teams_against` reports the unsupported entries; the supported vocabulary remains discoverable without being duplicated by callers.
- [ ] No credential, provider, or internal diagnostic material becomes reachable through the new field.
- [ ] The documented error contract is updated, and existing error-contract tests are extended rather than replaced.
- [ ] The existing generic message remains for anything that has no caller-actionable detail.

## Provenance

Raised during review of the frontend URL work (`crf04/statsplus-frontend#35`). Reviewers confirmed independently that `detail` is withheld from callers and that `public_details` is the intended home for exactly this. No frontend change depends on it: the frontend is correct as it stands, just less specific than it could be.

