"""Input validation and calendars; all resources ship in the image."""

import copy, hashlib, io, re, warnings, json

from datetime import date

from pathlib import Path
from PIL import Image

RESOURCES = Path(__file__).parent / "resources"

def read(path): return json.loads(path.read_text(encoding="utf-8"))

PRODUCTS = {
    'observed': 'Observed rainfall (% of average)',
    'short_term': 'Pure ten-day forecast',
    'mixed': 'One-month observations + forecast',
    'seasonal_rain': 'Seasonal rainfall forecast',
    'seasonal_temperature': 'Seasonal temperature forecast',
    'other': 'Other / not yet identified',
}

def suggested_product(filename):
    """Only our explicit product prefix is a suggestion; ambiguous WFP names stay unknown."""
    match = re.fullmatch(r'\d{2}_(observed|short_term|mixed|seasonal_rain|seasonal_temperature)__.+', Path(filename).name)
    return match.group(1) if match else 'other'

def region_option(region):
    code = 'AFY' if region['region_id'] == 'eastern_africa_yemen' else region['map_codes'][0]
    return code+' — '+region['label']

def regions():
    result = copy.deepcopy(read(RESOURCES / 'calendar.json')['regions'])
    eastern = copy.deepcopy(next(r for r in result if r['region_id'] == 'eastern_africa'))
    eastern.update(region_id='eastern_africa_yemen', label='Eastern Africa and Yemen (AFY)')
    eastern['seasons'].append(dict(season_id='yemen_summer',
        label='Yemen summer rains (highlands; local calendars vary)', rainy_months=[7, 8, 9, 10]))
    result.append(eastern)
    return result

def season_mode(month, months):
    if month in months:
        return 'past_and_future'
    if month == ((months[0] - 2) % 12) + 1:
        return 'future_only'
    if month == months[-1] % 12 + 1:
        return 'past_only'
    return 'excluded'

def calendar_for(region, report_date):
    return [dict(season_id=s['season_id'], label=s['label'], rainy_months=s['rainy_months'],
                 mode=season_mode(report_date.month, s['rainy_months'])) for s in region['seasons']]

def checklist(calendar):
    modes = {s['mode'] for s in calendar}
    wanted = set()
    if modes & {'past_and_future', 'past_only'}:
        wanted.add('observed')
    if modes & {'past_and_future', 'future_only'}:
        wanted.update(('short_term', 'mixed', 'seasonal_rain', 'seasonal_temperature'))
    return [k for k in PRODUCTS if k in wanted]

def inspect_image(data, name):
    if not data or len(data) > 30_000_000:
        raise ValueError('Each image must contain between 1 byte and 30 MB.')
    if Path(name).suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
        raise ValueError('Use PNG, JPEG or WebP images.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(data)) as im:
                if im.format not in ('PNG', 'JPEG', 'WEBP') or getattr(im, 'n_frames', 1) != 1:
                    raise ValueError('Use a static PNG, JPEG or WebP image.')
                if im.width * im.height > 45_000_000:
                    raise ValueError('An image exceeds 45 million pixels.')
                suffix = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[im.format]
                im.verify()
        except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError('An image cannot be decoded safely.') from exc
    return suffix


def prepare(region_id, report_date, notes, run_id, maps):
    region = next((r for r in regions() if r['region_id'] == region_id), None)
    if region is None:
        raise ValueError('Select a region explicitly.')
    cutoff = date.fromisoformat(report_date)
    if cutoff > date.today():
        raise ValueError('The availability cutoff cannot be in the future.')
    calendar = calendar_for(region, cutoff)
    if all(s['mode'] == 'excluded' for s in calendar):
        raise ValueError('All seasonal cycles are excluded for this month.')
    if len(notes) > 10000:
        raise ValueError('Input notes exceed 10,000 characters.')
    case_id = f'{report_date}__{region_id}__{run_id[:8]}'
    figures = []
    for i, m in enumerate(maps, 1):
        if m.get('issue_date') and date.fromisoformat(m['issue_date']) > cutoff:
            raise ValueError('An issue date is later than the availability cutoff.')
        figures.append(dict(figure_id=f'{case_id}__f{i:02}', path=m['object']['key'], sha256=m['object']['sha256'],
            metadata_note=PRODUCTS[m['product']] + '. ' + m.get('note', '') +
            (' Analyst-declared issue date: ' + m['issue_date'] + '.' if m.get('issue_date') else '')))
    missing = [PRODUCTS[k] for k in checklist(calendar) if k not in {m['product'] for m in maps}]
    limitations = ['Checklist not supplied / classified: ' + ', '.join(missing)] if missing else []
    if region_id == 'eastern_africa_yemen':
        limitations.append('AFY scope is Eastern Africa and Yemen only, without a Horn of Africa section. '
                           'Yemen calendar represents highland summer rains; local calendars vary.')
    if region_id == 'horn_of_africa' and any('AFY' in m['name'].upper() for m in maps):
        limitations.append('AFY-named maps supplied with Horn of Africa scope: verify geographic relevance.')
    return dict(case_id=case_id, report_date=report_date, report_month=report_date[:7], region_id=region_id, region_label=region['label'],
                calendar=calendar, figure_ids=[f['figure_id'] for f in figures], figures=figures,
                availability_note=f'Report date and availability cutoff: {report_date}. {notes}', limitations=limitations)
