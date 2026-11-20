use agent_client_protocol::v1::schema::{SessionConfigKind, SessionConfigOptionCategory};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Position};
use ratatui::style::Stylize;
use ratatui::text::{Line, Text};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};

use super::App;
use crate::options;

impl App {
    pub fn draw(&mut self, frame: &mut Frame) {
        let area = frame.area();
        let sidebar_width = if self.preferences.sidebar && area.width >= 72 {
            self.preferences.sidebar_width(area.width)
        } else {
            0
        };
        let (area, sidebar) = if sidebar_width > 0 {
            let [main, sidebar] =
                Layout::horizontal([Constraint::Min(40), Constraint::Length(sidebar_width)])
                    .areas(area);
            (main, Some(sidebar))
        } else {
            (area, None)
        };
        let prompt_height = area.height.clamp(4, 12) / 2;
        let approval_height = self.approvals.front().map_or(0, |(options, _)| {
            u16::try_from(options.len().min(9) + 1).unwrap_or(10)
        });
        let completion_height = self.completion.as_ref().map_or(0, |completion| {
            u16::try_from(completion.items.len().min(6)).unwrap_or(6)
        });
        let [header, transcript, approval, suggestions, composer, help] = Layout::vertical([
            Constraint::Length(1),
            Constraint::Min(1),
            Constraint::Length(approval_height),
            Constraint::Length(completion_height),
            Constraint::Length(prompt_height),
            Constraint::Length(1),
        ])
        .areas(area);

        frame.render_widget(
            Paragraph::new(Line::from(vec![
                " hrns ".bold().cyan(),
                format!(" · {} · {}", self.destination, self.status).into(),
                self.workspace
                    .as_ref()
                    .map_or_else(String::new, |cwd| format!(" · {cwd}"))
                    .into(),
                self.options
                    .iter()
                    .find_map(|option| match &option.kind {
                        SessionConfigKind::Select(select)
                            if options::matches_category(
                                option,
                                &SessionConfigOptionCategory::Model,
                            ) =>
                        {
                            Some(format!(" · {}", select.current_value))
                        }
                        _ => None,
                    })
                    .unwrap_or_default()
                    .into(),
                self.usage
                    .map_or_else(String::new, |(used, size)| {
                        format!(" · {used}/{size} tokens")
                    })
                    .into(),
                if self.attachment_reads > 0 {
                    " · Loading attachment".into()
                } else if self.attachments.is_empty() {
                    String::new().into()
                } else {
                    format!(" · {} attached", self.attachments.len()).into()
                },
                if self.queue.is_empty() {
                    String::new().into()
                } else {
                    let state = if self.queue_paused {
                        "queued · paused"
                    } else {
                        "queued"
                    };
                    format!(" · {} {state}", self.queue.len()).into()
                },
            ])),
            header,
        );
        let visible = self.transcript.visible(transcript.width, transcript.height);
        frame.render_widget(Paragraph::new(visible), transcript);

        if let Some((options, _)) = self.approvals.front() {
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

        if let Some(completion) = &self.completion {
            let rows: Vec<_> = completion
                .items
                .iter()
                .take(6)
                .enumerate()
                .map(|(index, suggestion)| {
                    let text = format!(
                        " {}  {}",
                        suggestion.insert.trim_end(),
                        suggestion.description
                    );
                    if index == completion.selected {
                        Line::from(text).cyan().bold()
                    } else {
                        Line::from(text).gray()
                    }
                })
                .collect();
            frame.render_widget(Paragraph::new(rows), suggestions);
        }

        let title = self.session.as_ref().map_or_else(
            || " Prompt ".to_owned(),
            |session| format!(" Prompt · {session} "),
        );
        let block = Block::default().borders(Borders::TOP).title(title).cyan();
        let input = block.inner(composer);
        frame.render_widget(block, composer);
        if input.width > 0 && input.height > 0 {
            let (rows, (row, column)) = self.draft.display(input.width);
            let scroll = row.saturating_sub(input.height - 1);
            let text = Paragraph::new(Text::from(
                rows.into_iter().map(Line::raw).collect::<Vec<_>>(),
            ));
            frame.render_widget(text.scroll((scroll, 0)), input);
            frame.set_cursor_position(Position::new(input.x + column, input.y + row - scroll));
        }
        frame.render_widget(
            Paragraph::new(if self.status == "Disconnected" {
                " Ctrl-R reconnect · draft is kept · Ctrl-B panel"
            } else {
                " Enter send · Shift+Enter newline · Tab complete · PgUp/PgDn scroll · Esc cancel · Ctrl-O tools · Ctrl-B panel · Ctrl-R reconnect"
            })
            .gray(),
            help,
        );
        if let Some(sidebar) = sidebar {
            self.draw_sidebar(frame, sidebar);
        }
    }

    fn draw_sidebar(&self, frame: &mut Frame, area: ratatui::layout::Rect) {
        let mut lines = vec![
            Line::from("Connection").cyan().bold(),
            Line::from(self.destination.clone()).gray(),
            Line::from(self.status).gray(),
        ];
        if let Some(workspace) = &self.workspace {
            lines.push(Line::from(workspace.clone()).gray());
        }
        if let Some(session) = &self.session {
            lines.push(Line::from(session.clone()).gray());
        }
        if let Some(model) = self.options.iter().find_map(|option| match &option.kind {
            SessionConfigKind::Select(select)
                if options::matches_category(option, &SessionConfigOptionCategory::Model) =>
            {
                Some(select.current_value.clone())
            }
            _ => None,
        }) {
            lines.push(Line::from(format!("model {model}")).gray());
        }
        if let Some((used, size)) = self.usage {
            lines.push(Line::from(format!("usage {used}/{size}")).gray());
        }
        lines.push(Line::from(""));
        lines.push(Line::from("Plan").cyan().bold());
        if self.plan.is_empty() {
            lines.push(Line::from("no plan").gray());
        } else {
            lines.extend(self.plan.iter().cloned().map(Line::from));
        }
        if !self.queue.is_empty() {
            lines.push(Line::from(""));
            lines.push(Line::from("Queue").cyan().bold());
            lines.push(Line::from(format!("{} waiting", self.queue.len())).gray());
        }
        if !self.skills.is_empty() {
            lines.push(Line::from(""));
            lines.push(Line::from("Skills").cyan().bold());
            lines.extend(
                self.skills
                    .iter()
                    .filter(|skill| skill.user_invocable)
                    .map(|skill| Line::from(format!("${}", skill.id)).gray()),
            );
        }
        if !self.activity.is_empty() {
            lines.push(Line::from(""));
            lines.push(Line::from("Activity").cyan().bold());
            lines.extend(self.activity.iter().cloned().map(Line::from));
        }
        let block = Block::default().borders(Borders::LEFT).title(" Panel ");
        frame.render_widget(
            Paragraph::new(lines)
                .block(block)
                .wrap(Wrap { trim: false }),
            area,
        );
    }
}
