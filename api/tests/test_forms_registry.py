"""The form registry: AcroForm and position-file imports, immutable versions and partner bundles."""
import json
import shutil

import pytest
import sqlalchemy as sa

from api.app import schema
from api.app.forms import importer, model, renderer
from api.app.forms.exchange import check_bundle
from api.app.forms.store import FormConflict, FormStore, pack_backgrounds
from api.tests.forms_fixtures import SVG, VALUES, fillable_pdf, positions_json
from api.tests.test_schema import database  # noqa: F401 - fixture


needs_gs = pytest.mark.skipif(shutil.which('gs') is None, reason='Ghostscript is not installed')


@pytest.fixture
def store(database):  # noqa: F811
    schema.upgrade_schema(database)
    return FormStore(database)


def svg_form():
    return importer.from_template(SVG, file_name='referral.svg', positions=positions_json())


@needs_gs
def test_a_fillable_pdf_imports_every_field_type_from_its_widgets():
    imported = importer.from_template(fillable_pdf())
    assert imported.source == 'pdf_acroform' and imported.media_type == 'application/pdf'
    fields = {item['name']: item for item in imported.content['fields']}
    assert {name: item['type'] for name, item in fields.items()} == {
        'patient': 'text', 'urgent': 'checkbox', 'clinic': 'choice', 'visit': 'choice', 'born': 'date',
        'amount': 'number', 'signed': 'signature'}
    assert fields['clinic']['options'] == ['North', 'South'] and fields['clinic']['option_boxes'] == {}
    assert fields['visit']['options'] == ['new', 'followup'] and set(fields['visit']['option_boxes']) == {'new', 'followup'}
    assert fields['born']['format'] == 'DD/MM/YYYY' and fields['amount']['decimals'] == 2
    assert fields['patient']['font_size'] == 11
    # The text field's rectangle (180, 672)-(480, 692) on a 612 x 792 pt page, in fax dots from the top-left.
    assert fields['patient']['box'] == [508, 272, 848, 55]


@needs_gs
def test_values_already_in_the_pdf_are_not_drawn_into_the_background():
    with_value = importer.from_template(fillable_pdf(prefilled='PREFILLED'))
    without = importer.from_template(fillable_pdf(prefilled=''))
    assert with_value.backgrounds[0].packed() == without.backgrounds[0].packed()
    assert with_value.address == without.address


@needs_gs
def test_a_pdf_with_a_position_file_uses_the_positions():
    positions = json.dumps({'fields': [{'name': 'note', 'type': 'text', 'x': 1, 'y': 1, 'width': 2, 'height': 1,
                                        'unit': 'in'}]})
    imported = importer.from_template(fillable_pdf(), positions=positions)
    assert imported.source == 'pdf_positions'
    assert imported.content['fields'][0]['box'] == [203, 196, 407, 196]


def test_an_svg_template_needs_positions_and_refuses_what_it_cannot_draw():
    with pytest.raises(model.FormError, match='needs a field-position file'):
        importer.from_template(SVG, file_name='referral.svg')
    arc = SVG.replace(b'<circle', b'<path d="M 10 10 A 5 5 0 0 1 20 20"/><circle')
    with pytest.raises(model.FormError, match='curved arcs'):
        importer.from_template(arc, file_name='referral.svg', positions=positions_json())
    picture = SVG.replace(b'<circle', b'<image href="x.png"/><circle')
    with pytest.raises(model.FormError, match='image element'):
        importer.from_template(picture, file_name='referral.svg', positions=positions_json())
    with pytest.raises(model.FormError, match='PDF or an SVG'):
        importer.from_template(b'GIF89a', file_name='form.gif')


def test_versions_are_immutable_and_identical_content_is_one_version(store):
    first, created = store.add_version(svg_form(), name='Referral')
    assert created and first.number == 1 and first.title == 'Referral'
    with store.engine.connect() as connection:
        before = dict(connection.execute(sa.select(store.versions)).mappings().one())
    again, created = store.add_version(svg_form(), name='Another name')
    assert not created and again.id == first.id
    changed = importer.from_template(SVG.replace(b'Synthetic referral form', b'Synthetic referral form 2'),
                                     file_name='referral.svg', positions=positions_json())
    second, created = store.add_version(changed, form_id=first.form['id'])
    assert created and second.number == 2 and second.address != first.address
    with store.engine.connect() as connection:
        rows = {row['id']: dict(row) for row in connection.execute(sa.select(store.versions)).mappings()}
    assert rows[first.id] == before
    assert [version['number'] for version in store.list_forms()[0]['versions']] == [1, 2]
    assert store.latest(first.form['id']).id == second.id
    # The registry has no way to change or remove a version.
    assert not any(name.startswith(('update', 'delete', 'remove', 'replace')) for name in dir(store))


def test_a_stored_version_draws_the_same_pages_as_its_import(store):
    imported = svg_form()
    version, _ = store.add_version(imported, name='Referral')
    reloaded = store.version(version_id=version.id)
    values = model.values(reloaded.content, VALUES, drawable=renderer.missing_characters)
    assert (renderer.render(reloaded.content, reloaded.backgrounds, values).hashes
            == renderer.render(imported.content, imported.backgrounds, values).hashes)
    assert store.template(version.id) == (SVG, 'image/svg+xml')
    assert store.addresses() == [{'address': imported.address, 'title': 'Referral', 'version': 1}]


def test_a_partner_bundle_is_kept_only_when_its_address_and_pages_match(store):
    version, _ = store.add_version(svg_form(), name='Referral')
    bundle = version.bundle()
    checked, pages = check_bundle(json.loads(json.dumps(bundle)), version.address)
    assert pages[0].packed() == version.backgrounds[0].packed()
    with pytest.raises(model.FormError):
        check_bundle(bundle, 'f' * 64)
    tampered = json.loads(json.dumps(bundle))
    tampered['content']['fields'][0]['box'][0] += 1
    with pytest.raises(model.FormError, match='does not match its address'):
        check_bundle(tampered, version.address)
    other = importer.from_template(SVG.replace(b'Patient', b'Client'), file_name='x.svg', positions=positions_json())
    swapped = {**bundle, 'backgrounds': pack_backgrounds(other.backgrounds)}
    with pytest.raises(model.FormError, match='do not match'):
        check_bundle(swapped, version.address)


def test_a_form_from_a_partner_keeps_its_title_and_version_and_takes_no_local_versions(store):
    imported = svg_form()
    bundle = {'faxbot_form_bundle': 1, 'address': imported.address, 'title': 'Their referral', 'version': 3,
              'content': imported.content,
              'backgrounds': pack_backgrounds(imported.backgrounds)}
    peer = {'id': 'peer-1'}
    kept = store.add_partner_version(bundle, peer=peer)
    assert (kept.title, kept.number, kept.row['source'], kept.row['peer_id']) == ('Their referral', 3, 'partner', 'peer-1')
    assert store.add_partner_version(bundle, peer=peer).id == kept.id
    with pytest.raises(FormConflict, match="partner's form"):
        store.add_version(importer.from_template(SVG.replace(b'Born', b'DOB'), file_name='x.svg',
                                                 positions=positions_json()), form_id=kept.form['id'])
