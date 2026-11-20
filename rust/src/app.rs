use futures::channel::{mpsc, oneshot};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Position};
use ratatui::style::{Color, Style, Stylize};
use ratatui::text::{Line, Text};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use unicode_width::UnicodeWidthStr;

use crate::acp::{Action, Event};

pub enum Key {
    Text(char),
    Enter,
    Newline,
    Backspace,
    ScrollUp,
    ScrollDown,
    Escape,
}

#[derive(PartialEq)]
enum Role {
    User,
    Agent,
    Thought,
    Note,
    Error,
}

struct Entry {
    role: Role,
    text: String,
}

type Approval = (Vec<String>, oneshot::Sender<Option<usize>>);

pub struct App {
    destination: String,
    status: &'static str,
    ready: bool,
    busy: bool,
    draft: String,
    history: Vec<Entry>,
    scroll_back: u16,
    approval: Option<Approval>,
    actions: mpsc::UnboundedSender<Action>,
}

impl App {
    pub fn new(destination: String, actions: mpsc::UnboundedSender<Action>) -> Self {
        Self {
            destination,
            status: "Connecting",
            ready: false,
            busy: false,
            draft: String::new(),
            history: Vec::new(),
            scroll_back: 0,
            approval: None,
            actions,
        }
    }

    pub fn event(&mut self, event: Event) {
        match event {
            Event::Ready => {
                self.ready = true;
                self.status = "Connected";
            }
            Event::Chunk(text) => self.append(Role::Agent, text),
            Event::Thought(text) => self.append(Role::Thought, text),
            Event::Note(text) => self.history.push(Entry {
                role: Role::Note,
                text,
            }),
            Event::Error(text) => self.history.push(Entry {
                role: Role::Error,
                text,
            }),
            Event::End => {
                self.busy = false;
                self.approval = None;
                self.status = "Connected";
            }
            Event::Disconnected => {
                self.ready = false;
                self.busy = false;
                self.approval = None;
                self.status = "Disconnected";
            }
            Event::Permission {
                title,
                options,
                respond,
            } => {
                self.history.push(Entry {
                    role: Role::Note,
                    text: format!("Permission requested · {title}"),
                });
                self.approval = Some((options, respond));
            }
        }
    }

    pub fn paste(&mut self, text: &str) {
        self.draft.extend(
            text.chars()
                .filter(|c| !c.is_control() || *c == '\n' || *c == '\t'),
        );
    }

    pub fn key(&mut self, key: Key) {
        if let Some((options, _)) = &self.approval {
            let answer = match key {
                Key::Escape => Some(None),
                Key::Text(digit) => digit
                    .to_digit(10)
                    .and_then(|digit| digit.checked_sub(1))
                    .map(|index| index as usize)
                    .filter(|index| *index < options.len())
                    .map(Some),
                _ => None,
            };
            if let Some(answer) = answer {
                if let Some((_, sender)) = self.approval.take() {
                    let _ = sender.send(answer);
                }
                return;
            }
        }

        match key {
            Key::Text(ch) => self.draft.push(ch),
            Key::Newline => self.draft.push('\n'),
            Key::Backspace => {
                self.draft.pop();
            }
            Key::ScrollUp => self.scroll_back = self.scroll_back.saturating_add(6),
            Key::ScrollDown => self.scroll_back = self.scroll_back.saturating_sub(6),
            Key::Escape if self.busy => {
                if self.actions.unbounded_send(Action::Cancel).is_ok() {
                    self.status = "Cancelling";
                }
            }
            Key::Enter if self.ready && !self.busy && !self.draft.trim().is_empty() => {
                if self
                    .actions
                    .unbounded_send(Action::Prompt(self.draft.clone()))
                    .is_ok()
                {
                    let text = std::mem::take(&mut self.draft);
                    self.history.push(Entry {
                        role: Role::User,
                        text,
                    });
                    self.busy = true;
                    self.scroll_back = 0;
                    self.status = "Streaming";
                }
            }
            Key::Escape | Key::Enter => {}
        }
    }

    fn append(&mut self, role: Role, text: String) {
        if let Some(entry) = self.history.last_mut().filter(|entry| entry.role == role) {
            entry.text.push_str(&text);
        } else {
            self.history.push(Entry { role, text });
        }
    }

    pub fn draw(&mut self, frame: &mut Frame) {
        let area = frame.area();
        let prompt_height = area.height.clamp(4, 12) / 2;
        let approval_height = self.approval.as_ref().map_or(0, |(options, _)| {
            u16::try_from(options.len().min(9) + 1).unwrap_or(10)
        });
        let [header, transcript, approval, composer, help] = Layout::vertical([
            Constraint::Length(1),
            Constraint::Min(1),
            Constraint::Length(approval_height),
            Constraint::Length(prompt_height),
            Constraint::Length(1),
        ])
        .areas(area);

        frame.render_widget(
            Paragraph::new(Line::from(vec![
                " hrns ".bold().cyan(),
                format!(" · {} · {}", self.destination, self.status).into(),
            ])),
            header,
        );
        let lines: Vec<Line<'_>> = self
            .history
            .iter()
            .flat_map(|entry| {
                let (label, color) = match entry.role {
                    Role::User => ("You", Color::Cyan),
                    Role::Agent => ("Agent", Color::Green),
                    Role::Thought => ("Thinking", Color::Gray),
                    Role::Note => ("", Color::Yellow),
                    Role::Error => ("Error", Color::Red),
                };
                let label = (!label.is_empty())
                    .then(|| Line::styled(label, Style::default().fg(color).bold()));
                label
                    .into_iter()
                    .chain(entry.text.lines().map(Line::raw))
                    .chain(std::iter::once(Line::default()))
            })
            .collect();
        let log = Paragraph::new(Text::from(lines)).wrap(Wrap { trim: false });
        let bottom = log
            .line_count(transcript.width)
            .saturating_sub(usize::from(transcript.height));
        let bottom = u16::try_from(bottom).unwrap_or(u16::MAX);
        self.scroll_back = self.scroll_back.min(bottom);
        frame.render_widget(log.scroll((bottom - self.scroll_back, 0)), transcript);

        if let Some((options, _)) = &self.approval {
            let choices = options
                .iter()
                .take(9)
                .enumerate()
                .map(|(i, option)| Line::from(format!(" {}  {option}", i + 1)));
            let rows: Vec<_> =
                std::iter::once(Line::from(" Choose a permission · Esc cancels".yellow()))
                    .chain(choices)
                    .collect();
            frame.render_widget(Paragraph::new(rows), approval);
        }

        let block = Block::default()
            .borders(Borders::TOP)
            .title(" Prompt ")
            .cyan();
        let input = block.inner(composer);
        frame.render_widget(block, composer);
        if input.width > 0 && input.height > 0 {
            let text = Paragraph::new(self.draft.as_str()).wrap(Wrap { trim: false });
            let (row, column) = cursor(&self.draft, input.width);
            let scroll = row.saturating_sub(input.height - 1);
            frame.render_widget(text.scroll((scroll, 0)), input);
            frame.set_cursor_position(Position::new(input.x + column, input.y + row - scroll));
        }
        frame.render_widget(
            Paragraph::new(" Enter send · Shift+Enter newline · PgUp/PgDn scroll · Esc cancel")
                .gray(),
            help,
        );
    }
}

fn cursor(text: &str, width: u16) -> (u16, u16) {
    let mut row = 0u16;
    let mut column = 0;
    for line in text.split('\n') {
        let columns = u16::try_from(line.width()).unwrap_or(u16::MAX);
        row = row.saturating_add(columns / width).saturating_add(1);
        column = columns % width;
    }
    (row.saturating_sub(1), column)
}
