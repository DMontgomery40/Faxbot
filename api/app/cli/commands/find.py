"""faxbot find: the console's page and settings finder, from the command line (GET /auth/finder).

Prints the same results the console's search box shows — the page or the one place a
setting is changed, its console address, its scope and the command that changes it —
from the same answer (admin_ui/src/__tests__/finder.json holds samples both surfaces
test against). Results are the ones this key may open; forbidden pages never appear.
"""
import typer

from .. import state

COMMAND = 'faxbot find'


def find_lines(words, results):
    """The lines `faxbot find WORDS` prints for one answer."""
    lines = [f'Find: {words}'] if words else ['Find']
    if not results:
        lines.append('Nothing matches. Try the words for the job, such as "retention", "T.38" or "who can send".')
    for hit in results:
        label = hit['label'] + (f" ({hit['name']})" if hit.get('name') else '')
        lines.append(f"  {label}")
        where = hit['place']
        if hit.get('address'):
            where += f" — {hit['address']}"
        lines.append(f"    {where}")
        lines.append(f"    Scope: {hit['scope']}")
        if hit.get('command'):
            lines.append(f"    {hit['command']}")
    return lines


def find_command(
    words: list[str] = typer.Argument(..., metavar='WORDS...',
                                      help='Words for the job or the technical name, such as retention, T.38 or "who can send".'),
):
    """Find the page that does a job, or the one place a setting is changed, by its words."""
    asked = ' '.join(words)
    answer = state.api().get('/auth/finder', params={'q': asked})

    def human(out):
        for line in find_lines(answer.get('q', asked), answer.get('results') or []):
            out.line(line)
    state.out().result(answer, human)
