import {EditorState} from '@codemirror/state';
import {EditorView, lineNumbers, highlightActiveLine, keymap, drawSelection} from '@codemirror/view';
import {defaultKeymap, history, historyKeymap, indentWithTab} from '@codemirror/commands';
import {defaultHighlightStyle, HighlightStyle, syntaxHighlighting, StreamLanguage} from '@codemirror/language';
import {tags} from '@lezer/highlight';
import {markdown} from '@codemirror/lang-markdown';
import {yamlFrontmatter} from '@codemirror/lang-yaml';
import {python} from '@codemirror/lang-python';
import {diff} from '@codemirror/legacy-modes/mode/diff';

export function editor(parent, text, {editable = false, path = '', patch = false, onChange = () => {}} = {}) {
  const language = patch ? StreamLanguage.define(diff) : path.endsWith('.py') ? python() : yamlFrontmatter({content: markdown()});
  const extensions = [lineNumbers(), drawSelection(), syntaxHighlighting(defaultHighlightStyle), language,
    EditorView.lineWrapping, EditorState.readOnly.of(!editable), EditorView.editable.of(editable),
    EditorView.contentAttributes.of({'aria-label': patch ? 'Plain patch' : editable ? 'Workflow source' : 'Read-only source'}),
    EditorView.updateListener.of(update => { if (update.docChanged) onChange(); })];
  if (patch) extensions.push(syntaxHighlighting(HighlightStyle.define([
    {tag: tags.inserted, color: '#18633a', backgroundColor: '#e5f5e9'},
    {tag: tags.deleted, color: '#982e26', backgroundColor: '#ffebe8'},
    {tag: tags.meta, color: '#315c8b'}
  ])));
  if (editable) extensions.push(history(), highlightActiveLine(), keymap.of([indentWithTab, ...defaultKeymap, ...historyKeymap]));
  const view = new EditorView({parent, state: EditorState.create({doc: text, extensions})});
  return {text: () => view.state.doc.toString(), destroy: () => view.destroy()};
}
