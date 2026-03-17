# Release Checklist

Before publishing a GitHub Release for HACS:

1. Update `custom_components/devi/manifest.json` `version`.
2. Confirm `hacs.json` and `README.md` still describe the published integration correctly.
3. Run HACS validation, hassfest, and tests successfully in GitHub Actions.
4. Create a Git tag in the format `vX.Y.Z` matching the manifest version.
5. Publish a GitHub Release from that tag.
6. Verify the GitHub Release tag, release title, and manifest version all match.
