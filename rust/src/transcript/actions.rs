use std::time::Duration;

use ratatui::style::Stylize;
use ratatui::text::Line;
use web_time::Instant;

use crate::theme::Palette;

pub(crate) const ACTION_FRAME_MILLIS: u128 = 100;
const ACTION_SPINNER: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

#[derive(Default)]
pub(super) struct ActionBlocks {
    blocks: Vec<ActionBlock>,
    active: bool,
    compact: bool,
}

struct ActionBlock {
    first: usize,
    last: usize,
    tail_start: usize,
    count: usize,
    expanded: bool,
    timing: Option<ActionTiming>,
}

#[derive(Clone, Copy)]
enum ActionTiming {
    Running(Instant),
    Finished(Duration),
}

#[derive(PartialEq, Eq)]
pub(super) enum ActionRows {
    Full,
    Preview,
    Hidden,
}

pub(super) struct ActionHeading {
    count: usize,
    expanded: bool,
    active: bool,
    timing: Option<ActionTiming>,
}

impl ActionHeading {
    pub(super) fn line(&self, palette: &Palette, animation_tick: Option<u128>) -> Line<'static> {
        let arrow = if self.expanded { "▾" } else { "▸" };
        let noun = if self.count == 1 { "action" } else { "actions" };
        let mut line = Line::from(
            format!("{arrow} {} {noun}", self.count)
                .fg(palette.muted)
                .bold(),
        );
        if let Some(timing) = self.timing {
            let elapsed = match timing {
                ActionTiming::Running(start) => start.elapsed(),
                ActionTiming::Finished(elapsed) => elapsed,
            };
            line.push_span(format!(" · {}", format_elapsed(elapsed)).fg(palette.muted));
        }
        if self.active
            && let Some(tick) = animation_tick
        {
            let spinner = ACTION_SPINNER[(tick % ACTION_SPINNER.len() as u128) as usize];
            line.push_span(" · ".fg(palette.muted));
            line.push_span(format!("{spinner} Running").fg(palette.warning));
        }
        line
    }
}

fn format_elapsed(elapsed: Duration) -> String {
    let total = elapsed.as_secs();
    let seconds = total % 60;
    let minutes = total / 60;
    if minutes < 60 {
        format!("{minutes}m {seconds:02}s")
    } else {
        format!("{}:{:02}:{seconds:02}", minutes / 60, minutes % 60)
    }
}

impl ActionBlocks {
    pub(super) fn compact(&self) -> bool {
        self.compact
    }

    pub(super) fn animating(&self) -> bool {
        self.compact && self.active
    }

    pub(super) fn set_compact(&mut self, compact: bool) -> bool {
        if self.compact == compact {
            return false;
        }
        self.compact = compact;
        for block in &mut self.blocks {
            block.expanded = false;
        }
        true
    }

    pub(super) fn push(&mut self, index: usize, live: bool) -> usize {
        if self.active
            && let Some(block) = self.blocks.last_mut()
        {
            let dirty = if self.compact && !block.expanded && block.count >= 2 {
                block.tail_start
            } else {
                index
            };
            block.tail_start = block.last;
            block.last = index;
            block.count += 1;
            return dirty;
        }
        self.blocks.push(ActionBlock {
            first: index,
            last: index,
            tail_start: index,
            count: 1,
            expanded: false,
            timing: live.then(|| ActionTiming::Running(Instant::now())),
        });
        self.active = true;
        index
    }

    pub(super) fn finish(&mut self) -> Option<usize> {
        if !std::mem::take(&mut self.active) {
            return None;
        }
        let block = self.blocks.last_mut()?;
        block.expanded = false;
        if let Some(ActionTiming::Running(start)) = block.timing {
            block.timing = Some(ActionTiming::Finished(start.elapsed()));
        }
        self.compact.then_some(block.first)
    }

    pub(super) fn can_extend(&self, index: usize) -> bool {
        self.active || self.blocks.last().is_none_or(|block| index > block.last)
    }

    pub(super) fn trim(&mut self, from: usize) {
        // Replay boundaries always finish the previous block.
        self.blocks
            .truncate(self.blocks.partition_point(|block| block.first < from));
        self.active = false;
    }

    pub(super) fn discard_prefix(&mut self, from: usize) {
        let first = self.blocks.partition_point(|block| block.first < from);
        self.blocks.drain(..first);
        for block in &mut self.blocks {
            block.first -= from;
            block.last -= from;
            block.tail_start -= from;
        }
    }

    fn block_index(&self, index: usize) -> Option<usize> {
        let candidate = self.blocks.partition_point(|block| block.first <= index);
        candidate
            .checked_sub(1)
            .filter(|&candidate| index <= self.blocks[candidate].last)
    }

    pub(super) fn layout(&self, index: usize) -> (Option<ActionHeading>, ActionRows) {
        if !self.compact {
            return (None, ActionRows::Full);
        }
        let Some(block_index) = self.block_index(index) else {
            return (None, ActionRows::Full);
        };
        let block = &self.blocks[block_index];
        let active = self.active && block_index + 1 == self.blocks.len();
        let heading = (index == block.first).then_some(ActionHeading {
            count: block.count,
            expanded: block.expanded,
            active,
            timing: block.timing,
        });
        let rows = if block.expanded {
            ActionRows::Full
        } else if active && index >= block.tail_start {
            ActionRows::Preview
        } else {
            ActionRows::Hidden
        };
        (heading, rows)
    }

    pub(super) fn toggle(&mut self, index: usize) {
        if let Some(block_index) = self.block_index(index) {
            let block = &mut self.blocks[block_index];
            block.expanded = !block.expanded;
        }
    }

    pub(super) fn reveal(&mut self, index: usize) -> Option<usize> {
        if self.layout(index).1 != ActionRows::Hidden {
            return None;
        }
        let block_index = self.block_index(index)?;
        let block = &mut self.blocks[block_index];
        block.expanded = true;
        Some(block.first)
    }
}
