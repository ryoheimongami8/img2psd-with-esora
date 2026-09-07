"""Prompt text shared by every image backend.

This is a description of what the picture should look like, not of who renders
it, so it lives here rather than being copied into each provider module.
"""

KEY_PRESETS = {
    "green": ((0, 255, 0), "純緑 (#00FF00)"),
    "magenta": ((255, 0, 255), "純マゼンタ (#FF00FF)"),
    "blue": ((0, 0, 255), "純青 (#0000FF)"),
}


def key_bg_instruction(preset: str) -> str:
    name = KEY_PRESETS.get(preset, KEY_PRESETS["green"])[1]
    return (
        f"\n\n背景は必ず完全に均一な{name}の単色で塗りつぶすこと。"
        "背景にグラデーション・影・模様・テクスチャを一切入れない。"
        f"キャラクター本体には{name}系の色を一切使わないこと。"
        f"{name}の照り返しや反射光もキャラクターに乗せない。"
        "キャラクターの輪郭・ポーズ・構図は入力画像から変更しないこと。"
    )


def compose_prompt(prompt: str, key_preset: str = None) -> str:
    """Append the key-background instruction to an arbitrary prompt."""
    if not key_preset:
        return prompt
    return prompt + key_bg_instruction(key_preset)


def key_only_prompt(preset: str) -> str:
    """Prompt for the mask-source pass: replace the background, change nothing else.

    This is deliberately a separate request from the colourisation pass. Folding the
    two together saves a call but puts the key colour into the very pixels the output
    colours are taken from, and a key-coloured edge cannot be fully undone once it is
    there -- the model draws fine hair as a darkened background rather than as hair,
    so those pixels hold no foreground colour to recover.
    """
    name = KEY_PRESETS.get(preset, KEY_PRESETS["green"])[1]
    return (
        f"入力画像の背景だけを、完全に均一な{name}の単色に置き換えてください。"
        "キャラクター本体は一切変更しないこと。線・色・陰影・輪郭・ポーズ・構図・"
        "サイズ・位置をすべて入力画像のまま維持し、再解釈も再設計もしないこと。"
        + key_bg_instruction(preset)
    )
