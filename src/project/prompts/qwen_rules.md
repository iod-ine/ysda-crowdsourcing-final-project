Mandatory target and output rules (also apply to custom instructions above):
Omit uncertain candidates.
Return one tight bounding box per clearly identified animal, including multiple animals when present. Do not produce duplicate boxes for the same animal or boxes around unrelated background regions.
Return ONLY a JSON object with one key, "boxes", and no Markdown or explanation:
{"boxes": [[100, 200, 500, 700]]}
Each box is [xmin, ymin, xmax, ymax], with positive width and height.
Use relative coordinates from 0 to 1000 on each axis: (0, 0) is the top-left
and (1000, 1000) is the bottom-right of the full image. Do not use pixel coordinates.
Return {"boxes": []} if no butterfly or moth can be clearly identified.
Do not include confidence scores or labels.
