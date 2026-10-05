# Interface artwork

[Interface icons](../frontend/src/assets/icons/README.md) records icon sources, revisions and notices.
[The logo catalog](../frontend/src/assets/logos/catalog.json) records tool-mark sources,
credits, licenses, modifications and checksums. About renders that catalog.

When adding artwork, update its catalog entry and REUSE annotations. Keep required
notices in `LICENSES/`, which ships in the container. Use marks to identify tools,
not to imply endorsement or brand Lookout.

Preserve these asset-specific conditions:

- Python: official two-snakes artwork, original colors and ™; nominative use only.
- GNU: credit FSF and Aurélio A. Heckert. Share adaptations under CC-BY-SA-2.0.
- Go Gopher: retain both the CC-BY-4.0 attribution and Devicon MIT notice.
- Rust: follow the linked trademark policy as well as the artwork license.
- CMake: the [software license](https://cmake.org/licensing/) does not grant trademark rights.

The catalog records dark-mode changes and Tux's archived permission source.
Asset tests check hashes and inert SVG content, not legal permission.
