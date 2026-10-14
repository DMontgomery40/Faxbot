"""Write the emulator's synthetic test fax: a fax TIFF (1728 dots wide, 204 x 196 dpi, Group 4), one page per argument
line, each page saying it is a test from the SR140 stand-in. Needs Pillow (the Faxbot API image has it).

    python3 make_test_tiff.py fax/test-fax.tiff [PAGES]
"""
import sys

from PIL import Image, ImageDraw


def page(number, total):
    image = Image.new('1', (1728, 2200), 1)
    draw = ImageDraw.Draw(image)
    lines = ['Faxbot gateway test', f'Page {number} of {total}',
             'Sent by the SR140 stand-in (tools/sr140-emulator), synthetic content only.']
    for row, line in enumerate(lines):
        draw.text((120, 160 + row * 60), line, fill=0)
    draw.rectangle((100, 120, 1628, 400), outline=0, width=4)
    for row in range(10):
        draw.line((120, 600 + row * 120, 1608, 600 + row * 120), fill=0, width=2)
    return image


def main(argv):
    target = argv[1] if len(argv) > 1 else 'fax/test-fax.tiff'
    total = int(argv[2]) if len(argv) > 2 else 2
    frames = [page(number, total) for number in range(1, total + 1)]
    frames[0].save(target, format='TIFF', save_all=True, append_images=frames[1:], compression='group4',
                   dpi=(204, 196))
    print(target)


if __name__ == '__main__':
    main(sys.argv)
