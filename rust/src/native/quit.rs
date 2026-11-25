use crossterm::event::{KeyCode, KeyEvent};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::Line;
use ratatui::widgets::{Block, BorderType, Clear, Paragraph, Wrap};

use crate::acp::Event;
use crate::app::{App, draw_choice_list};
use crate::theme::Palette;

#[derive(Clone, Copy)]
enum Choice {
    LeaveRunning,
    StopAndQuit,
    Stay,
}

const CHOICES: [(&str, Choice); 3] = [
    ("Leave running", Choice::LeaveRunning),
    ("Stop and quit", Choice::StopAndQuit),
    ("Stay", Choice::Stay),
];

#[derive(Default)]
pub(super) enum Quit {
    #[default]
    Closed,
    Checking,
    Choose {
        error: Option<String>,
        selected: usize,
    },
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
            (Self::Checking, Event::QuitChecked(Ok(true))) => *self = Self::choose(None),
            (Self::Checking, Event::QuitChecked(Err(error)))
            | (Self::Stopping, Event::QuitStopped(Err(error))) => {
                *self = Self::choose(Some(error.clone()));
            }
            (Self::Checking | Self::Stopping, Event::Disconnected) => {
                *self = Self::choose(Some("Disconnected from the Harness server".into()));
            }
            _ => {}
        }
    }

    fn choose(error: Option<String>) -> Self {
        Self::Choose { error, selected: 0 }
    }

    pub fn key(&mut self, key: KeyEvent, app: &App) {
        if matches!(self, Self::Checking | Self::Choose { .. }) && key.code == KeyCode::Esc {
            *self = Self::Closed;
            return;
        }
        let Self::Choose { selected, .. } = self else {
            return;
        };
        let choice = match key.code {
            KeyCode::Up => {
                *selected = selected.saturating_sub(1);
                return;
            }
            KeyCode::Down => {
                *selected = (*selected + 1).min(CHOICES.len() - 1);
                return;
            }
            KeyCode::Enter => CHOICES[*selected].1,
            KeyCode::Char('l') => Choice::LeaveRunning,
            KeyCode::Char('s') => Choice::StopAndQuit,
            _ => return,
        };
        *self = match choice {
            Choice::LeaveRunning => Self::Exit,
            Choice::Stay => Self::Closed,
            Choice::StopAndQuit if app.stop_for_quit() => Self::Stopping,
            Choice::StopAndQuit => Self::choose(Some("Could not reach the Harness server".into())),
        };
    }

    pub fn draw(&self, frame: &mut Frame, palette: &Palette) {
        if matches!(self, Self::Closed | Self::Exit) {
            return;
        }
        let area = frame.area();
        let width = area.width.saturating_sub(2).min(70);
        let height = area.height.min(8);
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
        let inner = block.inner(popup);
        frame.render_widget(block, popup);
        match self {
            Self::Choose { error, selected } => {
                let hint_height = u16::from(inner.height > 1);
                let choice_height = (CHOICES.len() as u16).min(inner.height - hint_height);
                let [message, choices, hint] = Layout::vertical([
                    Constraint::Min(0),
                    Constraint::Length(choice_height),
                    Constraint::Length(hint_height),
                ])
                .areas(inner);
                let text = error
                    .as_deref()
                    .unwrap_or("This is the only session with a run in progress.");
                frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: true }), message);
                let options = CHOICES.iter().map(|(label, _)| Line::from(*label));
                draw_choice_list(frame, choices, options, Some(*selected), palette);
                frame.render_widget(
                    Paragraph::new("↑/↓ move · Enter choose · Esc stay").fg(palette.muted),
                    hint,
                );
            }
            Self::Checking => frame.render_widget(
                Paragraph::new(vec![
                    Line::from("Checking running sessions…"),
                    Line::from("Esc stay").fg(palette.muted),
                ]),
                inner,
            ),
            Self::Stopping => frame.render_widget(Paragraph::new("Stopping this session…"), inner),
            Self::Closed | Self::Exit => {}
        }
    }
}
