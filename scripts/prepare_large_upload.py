"""生成真实含图与文字、超过10MiB的公开PDF，供批量上传完整摄取验收。"""

from pathlib import Path

from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject


def main() -> None:
    """使用结构化PDF API写入图像资源与少量文字，避免制造海量无意义切片。"""
    destination = Path("artifacts/experiments/2026-10-03_multi-upload/samples/large-policy.pdf")
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    page = writer.pages[0]
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    image = DecodedStreamObject()
    # 未压缩RGB图像真实可渲染，文件大小不依赖无意义PDF尾部填充。
    image.set_data(bytes(range(256)) * (2048 * 2048 * 3 // 256))
    image.update({
        NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(2048), NameObject("/Height"): NumberObject(2048),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): writer._add_object(image)}),
    })
    stream = DecodedStreamObject()
    stream.set_data(
        b"q 240 0 0 240 60 350 cm /Im1 Do Q\nBT /F1 12 Tf 60 740 Td\n"
        b"(Lyra maintenance policy.) Tj\n0 -20 Td\n"
        b"(The Lyra maintenance window is Saturday from 22:20 to 22:50 UTC.) Tj\n0 -20 Td\n"
        b"(The rollback owner is the platform operations team.) Tj\nET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as target:
        writer.write(target)
    assert destination.stat().st_size > 10 * 1024 * 1024
    assert "22:20" in PdfReader(destination).pages[0].extract_text()
    print(f"公开样本PDF生成并验证：{destination.stat().st_size} bytes")


if __name__ == "__main__":
    main()
