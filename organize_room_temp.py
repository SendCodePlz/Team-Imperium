import os
import shutil
import pandas as pd

SRC = '/Users/sidharth/Desktop/Caterpillar/Coding/cleaned_dataset'
DST = '/Users/sidharth/Desktop/Caterpillar/Coding/room_temp_batteries'

ROOM_TEMP_BATTERIES = [
    'B0005', 'B0006', 'B0007', 'B0018',
    'B0025', 'B0026', 'B0027', 'B0028',
    'B0033', 'B0034', 'B0036',
]

README_MAP = {
    'B0005': 'README_05_06_07_18.txt',
    'B0006': 'README_05_06_07_18.txt',
    'B0007': 'README_05_06_07_18.txt',
    'B0018': 'README_05_06_07_18.txt',
    'B0025': 'README_25_26_27_28.txt',
    'B0026': 'README_25_26_27_28.txt',
    'B0027': 'README_25_26_27_28.txt',
    'B0028': 'README_25_26_27_28.txt',
    'B0033': 'README_33_34_36.txt',
    'B0034': 'README_33_34_36.txt',
    'B0036': 'README_33_34_36.txt',
}

meta = pd.read_csv(os.path.join(SRC, 'metadata.csv'))
sub = meta[meta['battery_id'].isin(ROOM_TEMP_BATTERIES)].copy()
sub = sub.sort_values(['battery_id', 'test_id']).reset_index(drop=True)

os.makedirs(DST, exist_ok=True)
sub.to_csv(os.path.join(DST, 'metadata.csv'), index=False)

for battery_id in ROOM_TEMP_BATTERIES:
    bdir = os.path.join(DST, battery_id)
    ddir = os.path.join(bdir, 'data')
    os.makedirs(ddir, exist_ok=True)

    bmeta = sub[sub['battery_id'] == battery_id].sort_values('test_id').reset_index(drop=True)
    bmeta.to_csv(os.path.join(bdir, 'metadata.csv'), index=False)

    readme_src = os.path.join(SRC, 'extra_infos', README_MAP[battery_id])
    shutil.copy2(readme_src, os.path.join(bdir, 'README.txt'))

    for fname in bmeta['filename']:
        src_csv = os.path.join(SRC, 'data', fname)
        dst_csv = os.path.join(ddir, fname)
        shutil.copy2(src_csv, dst_csv)

    print(f'{battery_id}: {len(bmeta)} cycles copied')

print(f'\nTotal: {len(sub)} cycles across {len(ROOM_TEMP_BATTERIES)} batteries')
print(f'Output: {DST}')
