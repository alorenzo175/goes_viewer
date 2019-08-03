import datetime as dt
import numpy as np
from PIL import Image
from pyproj import CRS, transform
import pyresample
import xarray as xr


CONTRAST = 105
G17_CORNERS = np.array(((-116, 38), (-102, 30)))
G16_CORNERS = np.array(((-116, 30), (-102, 38)))
web_mercator = CRS.from_epsg("3857")


def open_file(path, corners):
    ds = xr.open_dataset(path, engine="netcdf4")
    proj_info = ds.goes_imager_projection
    proj4_params = {
        "ellps": "WGS84",
        "a": proj_info.semi_major_axis,
        "b": proj_info.semi_minor_axis,
        "rf": proj_info.inverse_flattening,
        "proj": "geos",
        "lon_0": proj_info.longitude_of_projection_origin,
        "lat_0": 0.0,
        "h": proj_info.perspective_point_height,
        "x_0": 0,
        "y_0": 0,
        "units": "m",
        "sweep": proj_info.sweep_angle_axis,
    }

    crs = CRS.from_dict(proj4_params)
    bnds = transform(crs.geodetic_crs, crs, corners[:, 0], corners[:, 1])
    ds = ds.update(
        {
            "x": ds.x * proj_info.perspective_point_height,
            "y": ds.y * proj_info.perspective_point_height,
        }
    ).assign_attrs(crs=crs, proj4_params=proj4_params)

    xarg = np.nonzero(((ds.x >= bnds[0].min()) & (ds.x <= bnds[0].max())).values)[0]
    yarg = np.nonzero(((ds.y >= bnds[1].min()) & (ds.y <= bnds[1].max())).values)[0]
    return ds.isel(x=xarg, y=yarg)


def make_geocolor_image(ds):
    # Load the three channels into appropriate R, G, and B
    R = ds["CMI_C02"].data
    NIR = ds["CMI_C03"].data
    B = ds["CMI_C01"].data

    # Apply range limits for each channel. RGB values must be between 0 and 1
    R = np.clip(R, 0, 1)
    NIR = np.clip(NIR, 0, 1)
    B = np.clip(B, 0, 1)

    # Calculate the "True" Green
    G = 0.45 * R + 0.1 * NIR + 0.45 * B
    G = np.clip(G, 0, 1)

    # Apply the gamma correction
    gamma = 1 / 1.7
    R = np.power(R, gamma)
    G = np.power(G, gamma)
    B = np.power(B, gamma)

    cleanIR = ds["CMI_C13"].data
    ir_range = ds.max_brightness_temperature_C13.valid_range

    cleanIR = (cleanIR - ir_range[0]) / (ir_range[1] - ir_range[0])
    cleanIR = np.clip(cleanIR, 0, 1)
    cleanIR = 1 - cleanIR

    # Lessen the brightness of the coldest clouds so they don't appear so bright
    # when we overlay it on the true color image.
    cleanIR = cleanIR / 1.3

    # Maximize the RGB values between the True Color Image and Clean IR image
    RGB_ColorIR = np.dstack(
        [np.maximum(R, cleanIR), np.maximum(G, cleanIR), np.maximum(B, cleanIR)]
    )

    F = (259 * (CONTRAST + 255)) / (255.0 * 259 - CONTRAST)
    out = F * (RGB_ColorIR - 0.5) + 0.5
    out = np.clip(out, 0, 1)  # Force value limits 0 through 1.
    return out


def make_resample_params(ds, corners):
    # can be saved for later use to save time
    goes_area = pyresample.AreaDefinition(
        ds.platform_ID,
        "goes area",
        "goes-r",
        projection=ds.proj4_params,
        width=len(ds.x),
        height=len(ds.y),
        area_extent=(
            ds.x.min().item(),
            ds.y.min().item(),
            ds.x.max().item(),
            ds.y.max().item(),
        ),
    )
    pts = transform(ds.crs.geodetic_crs, web_mercator, corners[:, 0], corners[:, 1])
    webm_area = pyresample.AreaDefinition(
        "webm",
        "web  mercator",
        "webm",
        projection=web_mercator.to_proj4(),
        width=len(ds.x),
        height=len(ds.y),
        area_extent=(pts[0][0], pts[1][0], pts[0][1], pts[1][1]),
    )
    return pyresample.bilinear.get_bil_info(goes_area, webm_area, 6e3, neighbours=8)


def resample_image(resample_params, img_arr):
    shape = img_arr.shape[:-1]
    out = np.dstack(
        [
            pyresample.bilinear.get_sample_from_bil_info(
                img_arr[..., i].reshape(-1), *resample_params, shape
            )
            for i in range(3)
        ]
        + [np.ones(shape)]
    )
    return (np.ma.fix_invalid(out).filled(0) * 255).astype("uint8")


def make_img_filename(ds):
    date = dt.datetime.utcfromtimestamp(ds.t.item() / 1e9)
    return f'{ds.platform_ID}_{date.strftime("%Y%m%dT%H%M%SZ")}.png'


if __name__ == "__main__":
    from pathlib import Path

    goes_dir = Path("/storage/projects/goes_alg/goes_data/southwest_adj/")
    goes_files = sorted(list(goes_dir.glob("*L2-MC*.nc")))
    goes_file = goes_files[-1]

    ds = open_file(goes_file, G16_CORNERS)
    img = make_geocolor_image(ds)

    resample_params = make_resample_params(ds, G16_CORNERS)
    nimg = resample_image(resample_params, img)
    Image.fromarray(nimg).save(make_img_filename(ds), format="png", optimize=True)
