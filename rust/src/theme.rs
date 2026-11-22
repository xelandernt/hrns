use ratatui::style::Color;

#[derive(Clone, Copy, Debug)]
pub struct Palette {
    pub background: Color,
    pub foreground: Color,
    pub surface: Color,
    pub panel: Color,
    pub accent: Color,
    pub file_reference: Color,
    pub skill_reference: Color,
    pub muted: Color,
    pub success: Color,
    pub warning: Color,
    pub error: Color,
    pub border: Color,
}

pub const NAMES: [&str; 5] = [
    "hrns",
    "textual-dark",
    "textual-light",
    "ansi-dark",
    "ansi-light",
];

pub fn palette(name: &str) -> Palette {
    match name {
        "textual-dark" => Palette {
            background: rgb(0x1e, 0x1e, 0x1e),
            foreground: rgb(0xe0, 0xe0, 0xe0),
            surface: rgb(0x26, 0x26, 0x26),
            panel: rgb(0x2d, 0x2d, 0x2d),
            accent: rgb(0x01, 0x78, 0xd4),
            file_reference: rgb(0x72, 0xc4, 0xc0),
            skill_reference: rgb(0xef, 0xbe, 0x79),
            muted: rgb(0x8a, 0x8a, 0x8a),
            success: rgb(0x4e, 0xbf, 0x71),
            warning: rgb(0xd9, 0xa5, 0x61),
            error: rgb(0xd4, 0x7c, 0x7c),
            border: rgb(0x44, 0x44, 0x44),
        },
        "textual-light" => Palette {
            background: rgb(0xf5, 0xf5, 0xf5),
            foreground: rgb(0x1e, 0x1e, 0x1e),
            surface: rgb(0xea, 0xea, 0xea),
            panel: rgb(0xdd, 0xdd, 0xdd),
            accent: rgb(0x01, 0x5f, 0xa8),
            file_reference: rgb(0x00, 0x63, 0x5f),
            skill_reference: rgb(0x8a, 0x51, 0x00),
            muted: rgb(0x66, 0x66, 0x66),
            success: rgb(0x2e, 0x7d, 0x32),
            warning: rgb(0xb2, 0x6a, 0x00),
            error: rgb(0xc6, 0x28, 0x28),
            border: rgb(0xbb, 0xbb, 0xbb),
        },
        "ansi-dark" | "ansi-light" => Palette {
            background: Color::Reset,
            foreground: Color::Reset,
            surface: Color::Indexed(8),
            panel: Color::Indexed(8),
            accent: Color::Indexed(13),
            file_reference: Color::Indexed(14),
            skill_reference: Color::Indexed(11),
            muted: Color::Indexed(7),
            success: Color::Indexed(2),
            warning: Color::Indexed(3),
            error: Color::Indexed(1),
            border: Color::Indexed(8),
        },
        _ => Palette {
            background: rgb(0x0c, 0x0b, 0x0f),
            foreground: rgb(0xe4, 0xe0, 0xea),
            surface: rgb(0x14, 0x12, 0x18),
            panel: rgb(0x1a, 0x17, 0x1f),
            accent: rgb(0xb7, 0x9c, 0xff),
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
