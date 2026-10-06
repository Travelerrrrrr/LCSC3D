// SPDX-License-Identifier: AGPL-3.0-or-later
#pragma once
#include "core/ir/FootprintIR.h"
#include <cmath>

// EasyEDA's PCB coordinates grow downward. Altium's grow upward.
// The vendored IR builder preserves the PCB canvas axis, unlike its symbol builder.
inline void normalizeAltiumAxes(EasyKiConverter::IR::FootprintComponentIR& footprint) {
    auto point = [](QPointF& value) { value.setY(-value.y()); };
    auto points = [&](QList<QPointF>& values) { for (auto& value : values) point(value); };
    auto angle = [](double value) { return std::fmod(360.0 - std::fmod(value, 360.0), 360.0); };
    for (auto& pad : footprint.pads) {
        point(pad.position);
        points(pad.customShapePoints);
        pad.rotation = angle(pad.rotation);
    }
    for (auto& track : footprint.tracks) points(track.points);
    for (auto& hole : footprint.holes) point(hole.center);
    for (auto& circle : footprint.circles) point(circle.center);
    for (auto& rect : footprint.rectangles) {
        rect.bounds = QRectF(rect.bounds.x(), -rect.bounds.bottom(), rect.bounds.width(), rect.bounds.height());
        rect.rotation = angle(rect.rotation);
    }
    for (auto& arc : footprint.arcs) {
        point(arc.center);
        const double start = angle(arc.endAngle);
        arc.endAngle = angle(arc.startAngle);
        arc.startAngle = start;
    }
    for (auto& text : footprint.texts) {
        point(text.position);
        points(text.textPathPoints);
        text.rotation = angle(text.rotation);
    }
    for (auto& region : footprint.regions) points(region.vertices);
    for (auto& outline : footprint.outlines) points(outline.points);
    for (auto& model : footprint.models3d) {
        const auto translation = model.translation();
        model.setTranslation({translation.x, -translation.y, translation.z});
    }
}
