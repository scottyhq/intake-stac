import os.path
import warnings

import pystac
from intake.catalog import Catalog
from intake.catalog.local import LocalCatalogEntry
from pkg_resources import get_distribution

__version__ = get_distribution('intake_stac').version

# STAC catalog asset 'type' determines intake driver:
# https://github.com/radiantearth/stac-spec/blob/master/item-spec/item-spec.md#media-types
default_type = 'application/rasterio'
default_driver = 'rioxarray'

drivers = {
    'application/netcdf': 'netcdf',
    'application/x-netcdf': 'netcdf',
    'application/parquet': 'parquet',
    'application/x-parquet': 'parquet',
    'application/x-hdf': 'netcdf',
    'application/x-hdf5': 'netcdf',
    'application/rasterio': 'rioxarray',
    'image/vnd.stac.geotiff': 'rioxarray',
    'image/vnd.stac.geotiff; cloud-optimized=true': 'rioxarray',
    'image/x.geotiff': 'rioxarray',
    'image/tiff; application=geotiff': 'rioxarray',
    'image/tiff; application=geotiff; profile=cloud-optimized': 'rioxarray',  # noqa: E501
    'image/jp2': 'rioxarray',
    'image/png': 'xarray_image',
    'image/jpg': 'xarray_image',
    'image/jpeg': 'xarray_image',
    'text/xml': 'textfiles',
    'text/plain': 'textfiles',
    'text/html': 'textfiles',
    'application/json': 'textfiles',
    'application/geo+json': 'geopandas',
    'application/geopackage+sqlite3': 'geopandas',
}


class AbstractStacCatalog(Catalog):

    version = __version__
    partition_access = False

    def __init__(self, stac_obj, **kwargs):
        """
        Initialize the catalog.

        Parameters
        ----------
        stac_obj: stastac.STACObject
            A pystac.STACObject pointing to a STAC object
        kwargs : dict, optional
            Passed to intake.Catalog.__init__
        """
        if isinstance(stac_obj, self._stac_cls):
            self._stac_obj = stac_obj
        elif isinstance(stac_obj, str):
            self._stac_obj = self._stac_cls.from_file(stac_obj)
        else:
            raise ValueError('Expected %s instance, got: %s' % (self._stac_cls, type(stac_obj)))

        metadata = self._get_metadata(**kwargs.pop('metadata', {}))
        try:
            name = kwargs.pop('name', self._stac_obj.id)
        except AttributeError:
            # Not currently tested.
            # ItemCollection does not require an id
            # Unclear what the state of ItemCollection is.
            name = str(type(self._stac_obj))

        super().__init__(name=name, metadata=metadata, **kwargs)

    @classmethod
    def from_url(cls, url, **kwargs):
        """
        Initialize the catalog from a STAC url.

        Parameters
        ----------
        url: str
            A URL pointing to a STAC json object
        kwargs : dict, optional
            Passed to intake.Catolog.__init__
        """
        stac_obj = cls._stac_cls.from_file(url)
        return cls(stac_obj, **kwargs)

    def _get_metadata(self, **kwargs):
        return kwargs  # pragma: no cover

    def serialize(self):
        """
        Serialize the catalog to yaml.

        Returns
        -------
        A string with the yaml-formatted catalog (just top-level).
        """
        return self.yaml()

    def stack_items(
        self, items, bands, path_as_pattern=None, concat_dim='band', override_coords=None
    ):
        """
        Experimental. create an xarray dataarray from a bunch of items

        currently not aware of CRS

        Get time coordinate from:
        item._stac_obj.datetime
        item.metadata['datetime']

        probably not very efficient / doesn't scale b/c opens files to read metadata in serial
        """
        # 1. iterate over items, call stack_bands()
        # 2. merge datasets
        # common name mapping from first item
        common2band = self[items[0]]._get_band_name_mapping(bands)
        hrefs = []
        for item in items:
            # NOTE: how to speed up this iteration?
            # https://github.com/intake/intake-stac/issues/66
            # print(self[item].metadata['proj:epsg'])
            # source = self[item].stack_bands(bands=bands)
            # call to_dask() within function and xarray merge?
            assets = self[item]._stac_obj.assets
            for band in bands:
                # same as stack_bands
                if band in assets:
                    asset = assets.get(band)
                elif band in common2band:
                    asset = assets.get(common2band[band])
                else:
                    raise ValueError(
                        f'Band "{band}" not found in asset ids\n({common2band.values()})\n \
                        or common_names\n({common2band.keys()})'
                    )
                print(asset.href)
                hrefs.append(asset.href)

        configDict = {}
        configDict['name'] = 'stack'
        configDict['description'] = 'stack of assets from multiple items'
        configDict['args'] = dict(
            chunks={},
            concat_dim=concat_dim,
            path_as_pattern=path_as_pattern,
            urlpath=hrefs,
            override_coords=override_coords,
        )
        configDict['metadata'] = {'items': items, 'bands': bands}

        return CombinedAssets(configDict)


class StacCatalog(AbstractStacCatalog):
    """
    Maps Intake Catalog to a STAC Catalog
    https://pystac.readthedocs.io/en/latest/api.html?#catalog-spec
    """

    # NOTE: name must match driver in setup.py entrypoints
    name = 'stac_catalog'
    _stac_cls = pystac.Catalog

    def _load(self):
        """
        Load the STAC Catalog.
        """
        for subcatalog in self._stac_obj.get_children():
            if isinstance(subcatalog, pystac.Collection):
                # Collection subclasses Catalog, so check it first
                driver = StacCollection
            else:
                driver = StacCatalog

            self._entries[subcatalog.id] = LocalCatalogEntry(
                name=subcatalog.id,
                description=subcatalog.description,
                driver=driver,  # recursive
                catalog=self,
                args={'stac_obj': subcatalog.get_self_href()},
            )

        for item in self._stac_obj.get_items():
            self._entries[item.id] = LocalCatalogEntry(
                name=item.id,
                description='',
                driver=StacItem,
                catalog=self,
                args={'stac_obj': item},
            )

    def _get_metadata(self, **kwargs):
        """
        Keep copy of all STAC JSON except for links
        """
        # NOTE: why not links?
        metadata = self._stac_obj.to_dict()
        del metadata['links']
        return metadata


class StacCollection(StacCatalog):
    """
    Maps Intake Catalog to a STAC Collection
    https://pystac.readthedocs.io/en/latest/api.html#collection-spec

    Collections have a number of properties that Catalogs do not, most notably
    the spatial and temporal extents. This is currently a placeholder for
    future Collection-specific attributes and methods.
    """

    name = 'stac_catalog'
    _stac_cls = pystac.Collection


class StacItemCollection(AbstractStacCatalog):
    """
    Maps ItemCollection returned from a STAC API to Intake (Sub)Catalog
    https://github.com/radiantearth/stac-api-spec/tree/master/fragments/itemcollection

    Note search results often use the single file stac extension:
    https://pystac.readthedocs.io/en/latest/api.html?#single-file-stac-extension
    """

    name = 'stac_item_collection'
    _stac_cls = pystac.Catalog

    def _load(self):
        """
        Load the STAC Item Collection.
        """
        if not self._stac_obj.ext.implements('single-file-stac'):
            raise ValueError("StacItemCollection requires 'single-file-stac' extension")
        for feature in self._stac_obj.ext['single-file-stac'].features:
            self._entries[feature.id] = LocalCatalogEntry(
                name=feature.id,
                description='',
                driver=StacItem,
                catalog=self,
                args={'stac_obj': feature},
            )

    def to_geopandas(self, crs=None):
        """
        Load the STAC Item Collection into a geopandas GeoDataFrame

        Parameters
        ----------
        crs : str or dict (optional)
              Coordinate reference system to set on the resulting frame.

        Returns
        -------
        GeoDataFrame

        """
        try:
            import geopandas as gpd
        except ImportError:
            raise ImportError(
                'Using to_geopandas requires the `geopandas` package.'
                'You can install it via Pip or Conda.'
            )

        if crs is None:
            crs = 'epsg:4326'
        gf = gpd.GeoDataFrame.from_features(self._stac_obj.to_dict(), crs=crs)
        return gf


class StacItem(AbstractStacCatalog):
    """
    Maps STAC Item to Intake (Sub)Catalog
    https://pystac.readthedocs.io/en/latest/api.html#item-spec
    """

    name = 'stac_item'
    _stac_cls = pystac.Item

    def _load(self):
        """
        Load the STAC Item.
        """
        for key, value in self._stac_obj.assets.items():
            self._entries[key] = StacAsset(key, value)

    def _get_metadata(self, **kwargs):
        metadata = self._stac_obj.properties.copy()
        for attr in ['bbox', 'geometry', 'datetime', 'date']:
            metadata[attr] = getattr(self._stac_obj, attr, None)
        metadata.update(kwargs)
        return metadata

    def _get_band_name_mapping(self, bands):
        """
        Return dictionary mapping common name to asset name
        eo:bands extension has [{'name': 'B01', 'common_name': 'coastal']
        return {'coastal':'B01'}
        """
        common2band = {}
        # 1. try to get directly from item metadata
        if 'eo' in self._stac_obj.stac_extensions:
            eo = self._stac_obj.ext['eo']
            for band in eo.bands:
                common2band[band.common_name] = band.name

        # 2. go a level up to collection metadata
        if common2band == {}:
            collection = self._stac_obj.get_collection()
            # Can simplify after item-assets extension implemented in Pystac
            # https://github.com/stac-utils/pystac/issues/132
            for asset, meta in collection.extra_fields['item_assets'].items():
                eo = meta.get('eo:bands')
                if eo:
                    for entry in eo:
                        common_name = entry.get('common_name')
                        if common_name:
                            common2band[common_name] = asset

        return common2band

    def stack_bands(self, bands, path_as_pattern=None, concat_dim='band', override_coords=None):
        """
        Stack the listed bands over the ``band`` dimension.

        This method only works for STAC Items using the 'eo' Extension
        https://github.com/radiantearth/stac-spec/tree/master/extensions/eo

        NOTE: This method is not aware of geotransform information. It *assumes*
        bands for a given STAC Item have the same coordinate reference system (CRS).
        This is usually the case for a given multi-band satellite acquisition.
        Coordinate alignment is performed automatically upon calling the
        `to_dask()` method to load into an Xarray DataArray if bands have diffent
        ground sample distance (gsd) or array shapes.

        Parameters
        ----------
        bands : list of strings representing the different bands
        (assset id or eo:bands "common_name" e.g. ['B4', B5'], ['red', 'nir'])

        Returns
        -------
        StacAsset with mapping of Asset names to Xarray bands

        Examples
        -------
        stack = item.stack_bands(['nir','red'])
        da = stack(chunks=dict(band=1, x=2048, y=2048)).to_dask()

        stack = item.stack_bands(['B4','B5'], path_as_pattern='{band}.TIF')
        da = stack(chunks=dict(band=1, x=2048, y=2048)).to_dask()
        """
        configDict = {}
        metadatas = {}
        item_metadata = self._stac_obj.properties
        hrefs = []
        common2band = self._get_band_name_mapping(bands)
        assets = self._stac_obj.assets
        for band in bands:
            if band in assets:
                asset = assets.get(band)
            elif band in common2band:
                asset = assets.get(common2band[band])
            else:
                raise ValueError(
                    f'Band "{band}" not found in asset ids\n({common2band.values()})\n \
                    or common_names\n({common2band.keys()})'
                )

            # map *HREF* to metadata to do fancy things when opening it
            asset_metadata = asset.properties
            metadatas[asset.href] = {**item_metadata, **asset_metadata}
            hrefs.append(asset.href)

        configDict['name'] = '_'.join(bands)
        configDict['description'] = ', '.join(bands)
        # NOTE: these are args for driver __init__ method
        configDict['args'] = dict(
            chunks={},
            concat_dim=concat_dim,
            path_as_pattern=path_as_pattern,
            urlpath=hrefs,
            override_coords=override_coords,
        )
        configDict['metadata'] = metadatas

        return CombinedAssets(configDict)


class StacAsset(LocalCatalogEntry):
    """
    Maps 1 STAC Item Asset to 1 Intake Catalog Entry
    https://pystac.readthedocs.io/en/latest/api.html#asset
    """

    name = 'stac_asset'
    _stac_cls = pystac.item.Asset

    def __init__(self, key, asset):
        """
        Construct an Intake catalog 'Source' from a STAC Item Asset.
        asset = pystac.item.Asset
        """
        driver = self._get_driver(asset)

        super().__init__(
            name=key,
            description=asset.title,
            driver=driver,
            direct_access=True,
            args=self._get_args(asset, driver),
            metadata=self._get_metadata(asset),
        )

    def _get_metadata(self, asset):
        """
        Copy STAC Asset Metadata and setup default plot
        """
        metadata = asset.to_dict()
        default_plot = self._get_plot(asset)
        if default_plot:
            metadata['plots'] = default_plot

        return metadata

    def _get_plot(self, asset):
        """
        Default hvplot plot based on Asset mimetype
        """
        # NOTE: consider geojson, parquet, hdf defaults in future...
        default_plot = None
        type = asset.media_type
        if type:
            if type in ['image/jpeg', 'image/jpg', 'image/png']:
                default_plot = dict(
                    thumbnail=dict(
                        kind='rgb',
                        x='x',
                        y='y',
                        bands='channel',
                        data_aspect=1,
                        flip_yaxis=True,
                        xaxis=False,
                        yaxis=False,
                    )
                )

            elif 'tiff' in type:
                default_plot = dict(
                    geotiff=dict(
                        kind='image',
                        x='x',
                        y='y',
                        frame_width=500,
                        data_aspect=1,
                        rasterize=True,
                        dynamic=True,
                        cmap='viridis',
                    )
                )

        return default_plot

    def _get_driver(self, asset):
        """
        Assign intake driver for data I/O
        """
        entry_type = asset.media_type

        if entry_type in ['', 'null', None]:

            suffix = os.path.splitext(asset.media_type)[-1]
            if suffix in ['.nc', '.h5', '.hdf']:
                asset.media_type = 'application/netcdf'
                warnings.warn(
                    f'STAC Asset "type" missing, assigning {entry_type} based on href suffix {suffix}:\n{asset.media_type}'  # noqa: E501
                )
            else:
                asset.media_type = default_type
                warnings.warn(
                    f'STAC Asset "type" missing, assuming default type={default_type}:\n{asset}'  # noqa: E501
                )
            entry_type = asset.media_type

        # if mimetype not registered try rioxarray driver
        driver = drivers.get(entry_type, default_driver)

        return driver

    def _get_args(self, asset, driver):
        """
        Optional keyword arguments to pass to intake driver
        """
        args = {'urlpath': asset.href}
        if driver in ['netcdf', 'rasterio', 'rioxarray', 'xarray_image']:
            # NOTE: force using dask?
            args.update(chunks={})

        return args


class CombinedAssets(LocalCatalogEntry):
    """
    Maps multiple STAC Item Assets to 1 Intake Catalog Entry
    """

    def __init__(self, configDict):
        """
        configDict = intake Entry dictionary from stack_bands() method
        """
        super().__init__(
            name=configDict['name'],
            description=configDict['description'],
            driver='rioxarray',
            direct_access=True,
            args=configDict['args'],
            metadata=configDict['metadata'],
        )
