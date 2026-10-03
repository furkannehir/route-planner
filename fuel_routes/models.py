from django.db import models


class FuelPriceRow(models.Model):
    """One CSV record, including records rejected by the import rules."""

    source_row_number = models.PositiveIntegerField(unique=True)
    opis_id = models.CharField(max_length=32, blank=True)
    station_name = models.CharField(max_length=255, blank=True)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=12, blank=True)
    rack_id = models.CharField(max_length=32, blank=True)
    retail_price = models.DecimalField(max_digits=12, decimal_places=8, null=True)
    status = models.CharField(max_length=24)
    station_key = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ['source_row_number']


class FuelStationQuerySet(models.QuerySet):
    def recommendable(self):
        return self.filter(
            location_type__in=['station', 'exit'],
            latitude__isnull=False,
            longitude__isnull=False,
        )


class FuelStation(models.Model):
    """A deduplicated station that can be considered by the route planner."""

    station_key = models.CharField(max_length=64, primary_key=True)
    opis_id = models.CharField(max_length=32)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=120)
    state = models.CharField(max_length=12, db_index=True)
    price_usd_per_gallon = models.DecimalField(max_digits=12, decimal_places=8)
    price_conflict = models.BooleanField(default=False)
    source_row_numbers = models.JSONField(default=list)
    latitude = models.FloatField(null=True)
    longitude = models.FloatField(null=True)
    location_type = models.CharField(max_length=16, db_index=True)
    location_source = models.CharField(max_length=32, blank=True)
    location_reference = models.CharField(max_length=128, blank=True)
    location_note = models.CharField(max_length=255, blank=True)

    objects = FuelStationQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=['location_type', 'state'])]

    @property
    def eligible_for_recommendation(self):
        return (
            self.location_type in {'station', 'exit'}
            and self.latitude is not None
            and self.longitude is not None
        )


class FuelImportRun(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    source_sha256 = models.CharField(max_length=64)
    exit_release = models.CharField(max_length=80)
    geonames_sha256 = models.CharField(max_length=64)
    coverage = models.JSONField()
