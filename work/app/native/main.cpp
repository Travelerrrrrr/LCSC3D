// SPDX-License-Identifier: AGPL-3.0-or-later
// Offline adapter around the unmodified EasyKiConverter Altium exporters.
#include "core/altium/ExporterAltiumFootprint.h"
#include "core/altium/ExporterAltiumSymbol.h"
#include "core/easyeda/EasyedaFootprintImporter.h"
#include "core/easyeda/EasyedaSymbolImporter.h"
#include "core/ir/FootprintDataConverter.h"
#include "core/ir/IrBuilder.h"
#include "normalize.h"

#include <QCommandLineParser>
#include <QCoreApplication>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <cstdio>

using namespace EasyKiConverter;

int main(int argc, char** argv) {
    QCoreApplication app(argc, argv);
    app.setApplicationName("lcsc-altium");
    app.setApplicationVersion("1.1.0");
    QCommandLineParser parser;
    parser.setApplicationDescription("Offline EasyEDA to Altium library converter");
    parser.addHelpOption();
    parser.addVersionOption();
    parser.addOptions({
        {"input", "EasyEDA component JSON", "file"},
        {"symbol", "Output SchLib", "file"},
        {"footprint", "Output PcbLib", "file"},
        {"lib-name", "Matching PcbLib name for the symbol", "name"},
        {"step", "STEP file to embed in the footprint", "file"},
    });
    parser.process(app);
    QFile input(parser.value("input"));
    if (!input.open(QIODevice::ReadOnly)) {
        std::fprintf(stderr, "Cannot open component JSON\n");
        return 2;
    }
    QJsonParseError parseError;
    const auto document = QJsonDocument::fromJson(input.readAll(), &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
        std::fprintf(stderr, "Invalid component JSON\n");
        return 2;
    }
    QJsonObject cad = document.object();
    if (cad.contains("result")) cad = cad["result"].toObject();
    QJsonObject result;
    QSharedPointer<FootprintData> footprint;
    if (!cad["packageDetail"].toObject()["dataStr"].toObject()["shape"].toArray().isEmpty())
        footprint = EasyedaFootprintImporter().importFootprintData(cad);

    if (parser.isSet("symbol")) {
        QJsonObject entry;
        if (cad["dataStr"].toObject()["shape"].toArray().isEmpty()) {
            entry["error"] = "Official library has no symbol data";
        } else {
            auto symbol = EasyedaSymbolImporter().importSymbolData(cad);
            auto ir = IR::toSymbolIR(*symbol);
            if (footprint && !footprint->info().name.isEmpty()) ir.footprintName = footprint->info().name;
            ExporterAltiumSymbol exporter;
            const bool ok = !ir.name.isEmpty() && exporter.exportSymbolLibrary(
                {ir}, parser.value("lib-name"), parser.value("symbol"), false, false);
            entry["ok"] = ok;
            entry["name"] = ir.name;
            entry["pins"] = ir.pins.size();
            entry["parts"] = ir.partCount;
            entry["footprint"] = ir.footprintName;
            if (!ok) entry["error"] = "Cannot write SchLib";
        }
        result["symbol"] = entry;
    }
    if (parser.isSet("footprint")) {
        QJsonObject entry;
        if (!footprint || !footprint->isValid()) {
            entry["error"] = "Official library has no valid footprint data";
        } else {
            bool hasStep = false;
            if (parser.isSet("step")) {
                QFile step(parser.value("step"));
                if (step.open(QIODevice::ReadOnly)) {
                    const auto bytes = step.readAll();
                    if (bytes.left(2048).contains("ISO-10303-21")) {
                        auto model = footprint->model3D();
                        model.setStep(bytes);
                        if (model.name().isEmpty()) model.setName(footprint->info().name);
                        footprint->setModel3D(model);
                        hasStep = true;
                    }
                }
            }
            auto ir = IR::toFootprintIR(*footprint);
            normalizeAltiumAxes(ir);
            ExporterAltiumFootprint exporter;
            const bool ok = exporter.exportFootprint(ir, parser.value("footprint"));
            entry["ok"] = ok;
            entry["name"] = ir.name;
            entry["pads"] = ir.pads.size();
            entry["step_embedded"] = hasStep;
            if (!ok) entry["error"] = "Cannot write PcbLib";
        }
        result["footprint"] = entry;
    }
    const auto output = QJsonDocument(result).toJson(QJsonDocument::Compact);
    std::fwrite(output.constData(), 1, output.size(), stdout);
    std::fputc('\n', stdout);
    return 0;
}
