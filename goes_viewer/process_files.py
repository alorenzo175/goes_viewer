import datetime as dt


import boto3
import numpy as np
from PIL import Image
from pyproj import CRS, transform
import pyresample
import s3fs
import xarray as xr


from goes_viewer.constants import (
    CONTRAST,
    G16_CORNERS,
    G17_CORNERS,
    WEB_MERCATOR,
    DX,
    DY,
)


def open_file(path, corners, engine='netcdf4'):
    ds = xr.open_dataset(path, engine=engine)
    proj_info = ds.goes_imager_projection
    proj4_params = {
        "ellps": "WGS84",
        "a": proj_info.semi_major_axis.item(),
        "b": proj_info.semi_minor_axis.item(),
        "rf": proj_info.inverse_flattening.item(),
        "proj": "geos",
        "lon_0": proj_info.longitude_of_projection_origin.item(),
        "lat_0": 0.0,
        "h": proj_info.perspective_point_height.item(),
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
    pts = transform(
        ds.crs.geodetic_crs, WEB_MERCATOR, sorted(corners[:, 0]), sorted(corners[:, 1])
    )
    width = int((pts[0][1] - pts[0][0]) / DX)
    height = int((pts[1][1] - pts[1][0]) / DY)
    webm_area = pyresample.AreaDefinition(
        "webm",
        "web  mercator",
        "webm",
        projection=WEB_MERCATOR.to_proj4(),
        width=width,
        height=height,
        area_extent=(pts[0][0], pts[1][0], pts[0][1], pts[1][1]),
    )
    shape = (height, width)
    return (
        pyresample.bilinear.get_bil_info(goes_area, webm_area, 6e3, neighbours=8),
        shape,
    )


def resample_image(resample_params, shape, img_arr):
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
    return f'figs/{ds.platform_ID}_{date.strftime("%Y-%m-%dT%H:%M:%SZ")}.png'


def get_s3_keys(bucket, timestamp=None, prefix="ABL-L2-MCMIPF"):
    """
    Generate the keys in an S3 bucket.

    :param bucket: Name of the S3 bucket.
    :param prefix: Only fetch keys that start with this prefix (optional).
    """
    s3 = boto3.client("s3")
    kwargs = {"Bucket": bucket}

    kwargs["Prefix"] = prefix
    if timestamp is not None:
        kwargs['Prefix'] += timestamp.strftime('%Y/%j/%H')

    while True:
        resp = s3.list_objects_v2(**kwargs)
        if resp["KeyCount"] == 0:
            break
        for obj in resp["Contents"]:
            key = obj["Key"]
            if key.startswith(prefix):
                yield key

        try:
            kwargs["ContinuationToken"] = resp["NextContinuationToken"]
        except KeyError:
            break


def process_s3_file(bucket, key):
    fs = s3fs.S3FileSystem(anon=True)
    remote_file = fs.open(f'{bucket}/{key}', mode='rb')
    if 'goes17' in bucket:
        corners = G17_CORNERS
    else:
        corners = G16_CORNERS
    ds = open_file(remote_file, corners, 'h5netcdf')
    img = make_geocolor_image(ds)
    resample_params, shape = make_resample_params(ds, G17_CORNERS)
    nimg = resample_image(resample_params, shape, img)
    Image.fromarray(nimg).save(make_img_filename(ds), format="png", optimize=True)


if __name__ == "__main__":
    import logging
    logging.basicConfig(format='%(asctime)s %(message)s', level='INFO')
    from pathlib import Path
    bucket = 'noaa-goes17'
    keys = get_s3_keys(bucket, prefix='ABI-L2-MCMIPF/2019/216')
    for key in keys:
        logging.info(key)
        process_s3_file(bucket, key)
    # goes_dir = Path("/storage/projects/goes_alg/full_disk")
    # goes_files = sorted(list(goes_dir.glob("*L2-MC*.nc")), reverse=True)
    # for goes_file in goes_files:
    #     ds = open_file(goes_file, G17_CORNERS)
    #     img = make_geocolor_image(ds)

    #     resample_params, shape = make_resample_params(ds, G17_CORNERS)
    #     nimg = resample_image(resample_params, shape, img)
    #     Image.fromarray(nimg).save(make_img_filename(ds), format="png", optimize=True)
