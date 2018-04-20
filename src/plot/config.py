from pyrocko.guts import Object, Float, Int, List, Tuple, String, load

from grond.meta import GrondError

guts_prefix = 'grond'


inch = 2.54


class PlotFormat(Object):

    @property
    def extension(self):
        return self.name

    def get_dpi(self, size_cm):
        return None


class PNG(PlotFormat):
    name = 'png'

    dpi = Float.T(optional=True)
    size_pixels = Int.T(optional=True)
    width_pixels = Int.T(optional=True)
    height_pixels = Int.T(optional=True)

    @property
    def extension(self):
        if self.dpi is not None:
            return 'd%i.png' % self.dpi
        elif self.size_pixels is not None:
            return 's%i.png' % self.size_pixels
        elif self.width_pixels is not None:
            return 'w%i.png' % self.width_pixels
        elif self.height_pixels is not None:
            return 'h%i.png' % self.height_pixels
        else:
            return 'd100.png'

    def get_dpi(self, size_cm):
        w_cm, h_cm = size_cm
        w_inch, h_inch = w_cm/inch, h_cm/inch
        if self.dpi:
            return self.dpi
        elif self.size_pixels is not None:
            return min(self.size_pixels/w_inch, self.size_pixels/h_inch)
        elif self.width_pixels is not None:
            return self.width_pixels/w_inch
        elif self.height_pixels is not None:
            return self.height_pixels/h_inch
        else:
            return 100.0


class PDF(PlotFormat):
    name = 'pdf'


class PlotConfig(Object):
    name = 'undefined'
    variant = String.T(default='default')
    formats = List.T(PlotFormat.T(), default=[PNG()])
    size_cm = Tuple.T(2, Float.T())
    font_size = Float.T(default=10.)

    @property
    def size_inch(self):
        return self.size_cm[0]/inch, self.size_cm[1]/inch


class PlotConfigCollection(Object):
    plot_configs = List.T(PlotConfig.T())


def read_plot_config_collection(path):
    collection = load(filename=path)
    if not isinstance(collection, PlotConfigCollection):
        raise GrondError(
            'invalid plot collection configuration in file "%s"' % path)

    return collection


__all__ = [
    'PlotFormat',
    'PNG',
    'PDF',
    'PlotConfig',
    'read_plot_config_collection',
]