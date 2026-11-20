use agent_client_protocol::v1::schema::{SessionConfigKind, SessionConfigOptionCategory};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Position};
use ratatui::style::Stylize;
use ratatui::text::{Line, Text};
use ratatui::widgets::{Block, Borders, Paragraph};

use super::App;
use crate::options;

impl App {
    pub fn draw(&mut self, frame: &mut Frame) {
        let area = frame.area();
        let prompt_height = area.height.clamp(4, 12) / 2;
        let approval_height = self.approvals.front().map_or(0, |(options, _)| {
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
                " Ctrl-R reconnect · draft is kept · PgUp/PgDn scroll"
            } else {
                " Enter send · Shift+Enter newline · PgUp/PgDn scroll · Esc cancel · Ctrl-R reconnect"
            })
            .gray(),
            help,
        );
    }
}
