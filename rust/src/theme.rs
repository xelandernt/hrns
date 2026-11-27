use ratatui::style::Color;

#[derive(Clone, Copy, Debug)]
pub struct Palette {
    pub background: Color,
    pub foreground: Color,
    pub surface: Color,
    pub panel: Color,
    pub accent: Color,
    pub system: Color,
    pub file_reference: Color,
    pub skill_reference: Color,
    pub muted: Color,
    pub success: Color,
    pub warning: Color,
    pub error: Color,
    pub border: Color,
}

pub const DEFAULT: &str = "dark";
pub const NAMES: [&str; 2] = ["dark", "light"];

pub fn palette(name: &str) -> Palette {
    match name {
        "light" => Palette {
            background: rgb(0xf8, 0xf7, 0xfb),
            foreground: rgb(0x1f, 0x1c, 0x26),
            surface: rgb(0xf0, 0xee, 0xf6),
            panel: rgb(0xe7, 0xe3, 0xf0),
            accent: rgb(0x6d, 0x4a, 0xc7),
            system: rgb(0x1f, 0x5f, 0xa8),
            file_reference: rgb(0x0f, 0x6f, 0x6b),
            skill_reference: rgb(0x9a, 0x5b, 0x0b),
            muted: rgb(0x67, 0x61, 0x73),
            success: rgb(0x28, 0x74, 0x3c),
            warning: rgb(0xa3, 0x68, 0x16),
            error: rgb(0xb8, 0x3b, 0x3b),
            border: rgb(0xd4, 0xce, 0xe0),
        },
        _ => Palette {
            background: rgb(0x0c, 0x0b, 0x0f),
            foreground: rgb(0xe4, 0xe0, 0xea),
            surface: rgb(0x14, 0x12, 0x18),
            panel: rgb(0x1a, 0x17, 0x1f),
            accent: rgb(0xb7, 0x9c, 0xff),
            system: rgb(0x73, 0xb8, 0xff),
            file_reference: rgb(0x75, 0xcb, 0xc9),
            skill_reference: rgb(0xed, 0xb8, 0x6e),
            muted: rgb(0x8d, 0x87, 0x98),
            success: rgb(0x7f, 0xb9, 0x8c),
            warning: rgb(0xd9, 0xa5, 0x61),
            error: rgb(0xd4, 0x7c, 0x7c),
            border: rgb(0x2d, 0x29, 0x33),
        },
    }
}

const fn rgb(red: u8, green: u8, blue: u8) -> Color {
    Color::Rgb(red, green, blue)
}
