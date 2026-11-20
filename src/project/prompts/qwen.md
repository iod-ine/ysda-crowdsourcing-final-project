Locate clearly identifiable butterflies and moths in this image. Prioritize avoiding false positives over detecting every possible target.

Some images contain no butterflies or moths. Do not assume a target is present.
Require visible features that identify a butterfly or moth; color, symmetry, or a vaguely wing-shaped outline alone are not sufficient.
Do not mistake other insects, leaves, flowers, bark patterns, or shadows for butterflies or moths.
If a candidate is too small, blurry, obscured, or ambiguous to identify, omit it rather than guessing.

Return one tight bounding box per clearly identified animal, including multiple animals when present. Do not produce duplicate boxes for the same animal or boxes around unrelated background regions.
If no candidate is clearly identifiable as a butterfly or moth, return {"boxes": []}.
