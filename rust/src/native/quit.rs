use crossterm::event::{KeyCode, KeyEvent};
use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Style, Stylize};
use ratatui::text::Line;
use ratatui::widgets::{Block, BorderType, Clear, Paragraph, Wrap};

use crate::acp::Event;
use crate::app::App;
use crate::theme::Palette;

#[derive(Default)]
pub(super) enum Quit {
    #[default]
    Closed,
    Checking,
    Choose(Option<String>),
    Stopping,
    Exit,
}

impl Quit {
    pub fn open(&mut self, app: &App) {
        *self = if app.check_quit() {
            Self::Checking
        } else {
            Self::Exit
        };
    }

    pub fn active(&self) -> bool {
        !matches!(self, Self::Closed)
    }

    pub fn finished(&self) -> bool {
        matches!(self, Self::Exit)
    }

    pub fn event(&mut self, event: &Event) {
        match (&self, event) {
            (Self::Checking, Event::QuitChecked(Ok(false)))
            | (Self::Stopping, Event::QuitStopped(Ok(()))) => *self = Self::Exit,
            (Self::Checking, Event::QuitChecked(Ok(true))) => *self = Self::Choose(None),
            (Self::Checking, Event::QuitChecked(Err(error)))
            | (Self::Stopping, Event::QuitStopped(Err(error))) => {
                *self = Self::Choose(Some(error.clone()));
            }
            (Self::Checking | Self::Stopping, Event::Disconnected) => {
                *self = Self::Choose(Some("Disconnected from the Harness server".into()));
            }
            _ => {}
        }
    }

    pub fn key(&mut self, key: KeyEvent, app: &App) {
        match (&self, key.code) {
            (Self::Checking | Self::Choose(_), KeyCode::Esc) => *self = Self::Closed,
            (Self::Choose(_), KeyCode::Char('l') | KeyCode::Enter) => *self = Self::Exit,
            (Self::Choose(_), KeyCode::Char('s')) => {
                *self = if app.stop_for_quit() {
                    Self::Stopping
                } else {
                    Self::Choose(Some("Could not reach the Harness server".into()))
                }
            }
            _ => {}
        }
    }

    pub fn draw(&self, frame: &mut Frame, palette: &Palette) {
        let lines = match self {
            Self::Checking => vec![
                Line::from("Checking running sessions…"),
                Line::from("Esc  Stay").fg(palette.muted),
            ],
            Self::Choose(error) => vec![
                error.as_ref().map_or_else(
                    || Line::from("This is the only session with a run in progress."),
                    |error| Line::from(error.as_str()),
                ),
                Line::default(),
                Line::from("Enter/L  Leave running     S  Stop and quit     Esc  Stay")
                    .fg(palette.accent),
            ],
            Self::Stopping => vec![Line::from("Stopping this session…")],
            Self::Closed | Self::Exit => return,
        };
        let area = frame.area();
        let width = area.width.saturating_sub(2).min(70);
        let height = area.height.saturating_sub(2).min(7);
        let popup = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        let block = Block::bordered()
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(palette.accent))
            .style(Style::default().fg(palette.foreground).bg(palette.surface))
            .title(" Quit Harness ");
        frame.render_widget(Clear, popup);
        frame.render_widget(
            Paragraph::new(lines).block(block).wrap(Wrap { trim: true }),
            popup,
        );
    }
}
