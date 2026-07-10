# Legal Notice

## Wizards of the Coast Fan Content Policy

> mtga-linux-exporter is unofficial Fan Content permitted under the
> [Fan Content Policy](https://company.wizards.com/en/legal/fancontentpolicy).
> Not approved/endorsed by Wizards. Portions of the materials used are
> property of Wizards of the Coast. ©Wizards of the Coast LLC.

Magic: The Gathering, MTG Arena, and all associated names, symbols, and
card text are trademarks and copyrighted material of **Wizards of the
Coast LLC**, a subsidiary of Hasbro, Inc. This project is not
affiliated with, endorsed by, sponsored by, or specifically approved by
Wizards of the Coast, Hasbro, or any of their affiliates.

This repository ships **no Wizards of the Coast trademarks, logos, mana
symbols, guild symbols, or card art assets**. Card metadata (names,
mana costs, oracle text, etc.) is read at runtime from the user's own
installed copy of MTG Arena; card images and format legality are
fetched at runtime from Scryfall's public API (see below). Sample JSON
files under `examples/` contain a handful of card records used solely
to demonstrate the tool's output shape — those records incorporate
Wizards' intellectual property under the Fan Content Policy attribution
quoted above.

## Scryfall

This project uses the [Scryfall API](https://scryfall.com/docs/api) for
card images and format legality data. Scryfall is an independent
project and has not endorsed, sponsored, or otherwise approved this
tool. All Scryfall guidelines apply to downstream users of the enriched
output:

- Do not modify, crop, distort, or watermark card images returned by
  Scryfall.
- Do not obscure or remove the artist credit or copyright text baked
  into the images.
- Do not use Scryfall data or images in a way that implies Scryfall
  endorses your work.
- Do not paywall access to Scryfall data.

Artist credits for each card are preserved in the enriched output as
the `artist` field (sourced from MTGA's own database) so that any
downstream UI can display them alongside the art.

## No warranty

The software is provided "as is", without warranty of any kind, express
or implied. Use at your own risk. The maintainer is not responsible for
any consequences arising from use of this tool, including but not
limited to changes to the MTG Arena client, Wizards' terms of service,
or account status.
