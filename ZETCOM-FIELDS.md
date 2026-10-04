# Zetcom / MuseumPlus RIA field cheatsheet

What the element and field names in a MuseumPlus RIA dump mean, in English and German.

**Provenance and how much to trust it.** This is *not* Zetcom's official schema. The names below are
the ones actually present in the imported data (`sync_Object` 1000, `sync_Person` 652,
`sync_Multimedia` 4232 records), and the English/German glosses are read off the names — Zetcom's
field naming is compositional, so most are unambiguous, but a few are guesses. Those are marked
**`?`**. The authoritative labels live in the MuseumPlus field configuration (or with the
registrars / the colleague); **correct anything here that reads wrong** — this file is a gloss, not a
source of truth.

## 1. The naming grammar

Almost every name is `<Prefix><Meaning><TypeSuffix>`. Learn the pieces and you can decode a field
that is not in the tables.

### Module / family prefixes

| Prefix | English | German | Example |
|---|---|---|---|
| `Obj` | Object | Objekt | `ObjObjectNumberTxt` |
| `Per` | Person | Person | `PerNennformTxt` |
| `Mul` | Multimedia / asset | Multimedia / Digitalisat | `MulOriginalFileTxt` |
| `Address` / `Addr` | Address | Adresse | `AddressTxt` |
| `Type` | Type / typology | Typ | `TypeDimRef` |
| `Inv` | Inventory | Inventar | `InvNumberSchemeRef` |
| `_User…` | **user-defined field** (this museum's own) | benutzerdefiniertes Feld | `_UserOalA04STxt` |
| `Akl…` | **AKL** project fields `?` (Allgemeines Künstlerlexikon) | AKL-Felder | `AklBiographieClb` |

### Field-type suffixes (Zetcom data-type codes)

| Suffix | English | German | Seen on |
|---|---|---|---|
| `Txt` | text | Text | `TitleTxt` |
| `Clb` | pick-list / code-list value `?` | Auswahlliste | `NotesClb` |
| `Lnu` | list/order number `?` | Listen-/Sortiernummer | `SortLnu` |
| `Dat` | date | Datum | `MulShootingDateDat` |
| `Tsp` | timestamp/period `?` | Zeitstempel | `DateTsp` |
| `Tst` | timestamp `?` | Zeitstempel | `ContentDateTst` |
| `Tim` | time | Uhrzeit | `TimeFromTim` |
| `Num` | number | Zahl | `HeightNum` |
| `Boo` | boolean (yes/no) | Ja/Nein | `ThumbnailBoo` |
| `Vrt` | **virtual** (computed for display, not stored) | virtuelles Feld | `ObjDateVrt` |
| `Voc` | vocabulary term (controlled list) | Vokabular | `MaterialVoc` |
| `Ref` | reference to another module | Referenz | `ObjOwnerRef` |
| `Grp` | repeatable group (a repeating block) | Wiederholungsgruppe | `ObjDateGrp` |
| `Cre` | composite (a container of fields) | Sammelgruppe | `ObjObjectCre` |
| `S` (e.g. `…NrSTxt`) | unknown `?` | – | `InventarNrSTxt` |

### XML element kinds (the shape, not the field)

| Element | English | German |
|---|---|---|
| `moduleItem` | one record | ein Datensatz |
| `dataField` | a normal field | Datenfeld |
| `virtualField` | computed field | virtuelles Feld |
| `systemField` | system metadata (`__lastModified` etc.) | Systemfeld |
| `vocabularyReference` | link to a controlled term | Vokabularverweis |
| `moduleReference` → `moduleReferenceItem` | link to another module's record | Modulverweis |
| `composite` | container of fields | Sammelgruppe |
| `repeatableGroup` → `repeatableGroupItem` | repeating block | Wiederholungsgruppe |
| `dataField/value`, `virtualField/value` | the value itself | der Wert |
| `formattedValue` | display form of a reference/term | Anzeigeform |

### System fields (`__` prefix, on every record)

| Field | English | German |
|---|---|---|
| `__id` | internal record id (our identifier key) | interne ID |
| `__uuid` | UUID (unreliable here) | UUID |
| `__orgUnit` | organisational unit | Organisationseinheit |
| `__created` / `__createdUser` | created at / by | angelegt am / von |
| `__lastModified` | last modified (**our datestamp**) | zuletzt geändert |
| `__lastModifiedUser` | modified by | geändert von |
| `__legacyId` | legacy id from the old system | Altsystem-ID |
| `__fieldsFilled` / `__referencesFilled` | counts of filled fields/links | Anzahl gefüllter Felder/Verweise |

## 2. Object module (`Obj…`)

### Identity and numbering

| Zetcom | English | German |
|---|---|---|
| `ObjObjectNumberTxt` | object number | Objektnummer |
| `ObjObjectNumberSortedTxt` | sortable object number | sortierbare Objektnummer |
| `InventarNrSTxt` / `InvNumberSchemeRef` | inventory number / scheme | Inventarnummer / -schema |
| `CatalogueNumberTxt` | catalogue number | Katalognummer |
| `NumberTxt` / `NumberMaxTxt` | number / max number | Nummer / Nummer (max) |
| `PrefixTxt` / `SuffixTxt` | number prefix / suffix | Nummer-Präfix / -Suffix |
| `Part1Txt` … `Part5Txt` | number parts 1–5 | Nummernteile 1–5 |
| `ObjBarcodeNumberVrt` | barcode number | Barcode-Nummer |
| `ObjObjectReferenceNumbersVrt` | all reference numbers | alle Referenznummern |

### Description

| Zetcom | English | German |
|---|---|---|
| `TitleTxt` | title | Titel |
| `ObjObjectTitleGrp` | title group | Titelgruppe |
| `ObjTechnicalTermClb` | technical term (object type) | Objektbezeichnung / Technik `?` |
| `TechnicalTermVoc` / `TechnicalTermTxt` | technical term (list / text) | Technik (Vokabular / Text) |
| `MaterialVoc` | material | Material |
| `TechniqueVoc` | technique | Technik |
| `ObjMaterialTechniqueGrp` / `ObjMaterialTechniqueVrt` | material & technique | Material/Technik |
| `MaterialVrt` / `TechniqueVrt` | material / technique (display) | Material / Technik |
| `DetailTxt` / `DetailsTxt` | detail / details | Detail / Details |
| `ObjIconographyContentBriefClb` | iconography (brief) | Ikonografie (Kurzfassung) |
| `ObjSystematicGrp` / `SystematicVoc` | classification | Systematik |
| `SystematicMultipleBoo` | multiple classification | mehrere Systematiken |
| `NotationTxt` | notation | Notation |
| `DenominationVoc` | denomination (what it is) | Bezeichnung |
| `ObjOrgGroupVoc` | **collection group — our `KK` set** | Sammlungsgruppe |

### Dimensions

| Zetcom | English | German |
|---|---|---|
| `HeightNum` | height | Höhe |
| `WidthNum` | width | Breite |
| `DepthNum` | depth | Tiefe |
| `DiameterNum` | diameter | Durchmesser |
| `ThicknessNum` | thickness | Stärke |
| `ObjDimAllGrp` | all dimensions | alle Maße |

### Dating

| Zetcom | English | German |
|---|---|---|
| `ObjDateGrp` / `ObjDateVrt` | dating / display date | Datierung |
| `DateTxt` / `DateDat` | date (text / date) | Datum |
| `DateFromTxt` / `DateFromDat` | date from | Datum von |
| `DateToTxt` / `DateToTsp` | date to | Datum bis |
| `DateTsp` | date (timestamp) | Datum (Zeitstempel) |
| `DatestampFromFuzzySearchLnu` / `…To…` | fuzzy date search bounds | unscharfe Datumssuche |
| `ObjInventoryDateDat` | inventory date | Inventarisierungsdatum |
| `CertaintyVoc` | dating certainty | Datierungssicherheit |

### People, ownership, provenance

| Zetcom | English | German |
|---|---|---|
| `ObjPerAssociationRef` | link to Person records | Verweis auf Personen |
| `ObjPerAssociationMainParticipantNameVrt` | main participant (name) | Hauptbeteiligte:r (Name) |
| `ObjPerAssociationSurnameVrt` | surname | Nachname |
| `ObjPerAssociationGenderVrt` | gender | Geschlecht |
| `ObjOwnerRef` | **owner / holding department** (Address module) | Eigentümer / besitzende Abteilung |
| `ObjOwnershipRef` / `ObjOwnership001Ref` | ownership history | Besitzverhältnisse |
| `ObjRegistrarRef` | registrar | Registrar:in |
| `ObjAcquisitionSourcePerRef` / `ObjAcquisitionSourceVoc` | acquisition source | Erwerbungsquelle |
| `ObjAcquisitionReferenceNrTxt` | acquisition reference number | Erwerbs-Referenznummer |
| `ObjAcquisitionDateGrp` / `ObjAccessionGrp` | acquisition date / accession | Erwerbsdatum / Zugang |
| `ObjCreditLineVoc` | credit line | Herkunftsangabe (Credit line) |
| `ObjProvBewertungVoc` | provenance assessment | Provenienzbewertung |
| `ObjLiteratureRef` | literature reference | Literatur |
| `ExhibitionRef` | exhibition reference | Ausstellung |
| `ObjConservationRef` / `ObjConservationTermsLoanVoc` | conservation / loan terms | Restaurierung / Leihbedingungen |

### Locations and movement

| Zetcom | English | German |
|---|---|---|
| `ObjCurrentLocationGrp` / `ObjCurrentLocationVrt` / `ObjCurrentLocationVoc` | current location | aktueller Standort |
| `ObjCurrentLocationHierarchicalVrt` | current location (hierarchy) | Standort (hierarchisch) |
| `ObjNormalLocationVrt` / `ObjNormalLocationVoc` | normal/home location | Normalstandort |
| `LocationVoc` | location | Standort |
| `MovementRef` / `ObjMovementRef` | movement / transport | Ortsveränderung / Transport |
| `ObjGeograficGrp` / `ObjGeograficVrt` / `GeopolVoc` | geography | Geografie |
| `ObjAssociatedPlaceVrt` | associated place | zugehöriger Ort |

### Multimedia, publication, admin

| Zetcom | English | German |
|---|---|---|
| `ObjMultimediaRef` | link to Multimedia records | Verweis auf Multimedia |
| `ObjMultimediaRestrictionsVrt` | multimedia restrictions | Multimedia-Einschränkungen |
| `ObjPublicationGrp` / `PublicationVoc` | publication | Publikation |
| `ObjPublicationStatusVoc` | publication status (**physical status**, not "published") | Publikationsstatus (physisch) |
| `PublicationVoc` | publication | Veröffentlichung |
| `ObjDashboardPublicationStatusVrt` | publication status (dashboard) | Publikationsstatus (Übersicht) |
| `ExportClb` / `ExportBoo` | export flag | Export-Kennzeichen |
| `ThumbnailBoo` | has thumbnail | Thumbnail vorhanden |
| `PreviewTxt` / `PreviewVrt` / `PreviewDateVrt` | preview | Vorschau |
| `PicturePageTxt` / `PageRefTxt` | picture page / page reference | Bildseite / Seitenverweis |
| `ObjRecordCreatedByTxt` / `ModifiedByTxt` / `ModifiedDateDat` | record created by / modified by / modified on | angelegt von / geändert von / geändert am |
| `ObjTextGrp` / `TextTxt` / `TextClb` / `TextHTMLClb` | text blocks | Textfelder |
| `ObjKeyWordsGrp` / `KeyWordVoc` | keywords | Schlagwörter |
| `ObjOtherNumberGrp` | other numbers | weitere Nummern |
| `ObjEditingGrp` / `EditingVoc` | editing | Bearbeitung |
| `ObjArchiveGrp` / `ArchiveVoc` | archive | Archiv |
| `ObjLabelObjectGrp` / `LabelClb` | label | Beschriftung |
| `ObjConditionGrp` / `ConditionClb` | condition | Zustand |
| `ObjResponsibleGrp` / `ResposibleVoc` *(sic)* | responsible | Verantwortlich |
| `ObjURLGrp` | URLs | URLs |
| `ObjRightsGrp` | rights | Rechte |

## 3. Person module (`Per…`)

| Zetcom | English | German |
|---|---|---|
| `PerNennformTxt` | preferred name form | Nennform |
| `PerNameTxt` / `NameTxt` | name | Name |
| `SortingLnu` / `SortLnu` | sorting | Sortierung |
| `KatNoTxt` | catalogue number | Katalognummer |
| `GNDTxt` / `GndVoc` | GND id | GND-Nummer |
| `ULANTxt` / `UlanVoc` | ULAN id | ULAN-Nummer |
| `VIAFTxt` / `ViafVoc` | VIAF id | VIAF-ID |
| `GNDCorporateTxt` / `CorporateGndVoc` | GND (corporate body) | GND (Körperschaft) |
| `PerGenderVoc` | gender | Geschlecht |
| `PerNationalityVoc` | nationality | Nationalität |
| `PerTypeVoc` | person type | Personentyp |
| `PerClassVoc` | class | Klasse |
| `PerOccupationGrp` / `Occupation…` | occupation | Beruf |
| `PerTitleGrp` / `TitleVoc` | title | Titel |
| `FunctionVoc` | function | Funktion |
| `PerDateGrp` / `DatingNewTxt` / `DateFromTxt` / `DateToTxt` | life dates / dating | Lebensdaten / Datierung |
| `PerGeograficGrp` / `GeograficVoc` | geography | Geografie |
| `PlaceTxt` / `PlaceNameVoc` | place | Ort |
| `ReligionTxt` | religion | Religion |
| `PerNameOtherGrp` | other name forms | weitere Namensformen |
| `PerBiographicalNoteGrp` / `PerNotesClb` | biographical note | biografische Notiz |
| `PerSourceClb` / `SourceTxt` / `SourceTxtVoc` | source | Quelle |
| `PerObjectRef` / `PerObjectgroupRef` | objects by/related to this person | Objekte zu dieser Person |
| `PerPhotographerRef` / `PerMultimediaRef` | assets (as photographer) | Multimedia (als Fotograf:in) |
| `PerLiteratureRef` / `PerOwnershipMNRef` / `PerVenderRef` | literature / ownership / vendor | Literatur / Besitz / Verkäufer:in |
| `PerStandardDataGrp` / `PerGeneralGrp` / `PerAddressGrp` / `AddressTxt` | address & general data | Adresse & allgemeine Daten |
| `AklBiographieClb` / `AklVitaClb` / `AklWvzClb` / `AklAuthorTxt` / `AklBiogrammClb` / `AklExhibitionClb` / `AklSourceClb` | **AKL** (Allgemeines Künstlerlexikon) project fields `?` | AKL-Projektfelder |
| `CollectionVoc` | collection | Sammlung |

## 4. Multimedia module (`Mul…`)

| Zetcom | English | German |
|---|---|---|
| `MulOriginalFileTxt` | original file name | Originaldatei |
| `MulOriginalFileLocationClb` | storage location | Speicherort |
| `MulSizeTxt` / `MulSizeLnu` / `MulSizeMBNum` | file size (MB) | Dateigröße (MB) |
| `MulSHA1Txt` | SHA-1 checksum | SHA1-Prüfsumme |
| `MulUseTxt` | use | Verwendung |
| `MulReferenceNumberTxt` | reference number | Referenznummer |
| `MulSubjectTxt` | subject | Thema |
| `MulDateTxt` / `MulShootingDateDat` | date / shooting date | Datum / Aufnahmedatum |
| `MulDateExifTst` / `MulEXIFGrp` | EXIF date / EXIF | EXIF-Datum / EXIF |
| `MulIPTCUrheberTxt` | IPTC creator (photographer/rights holder) | IPTC Urheber |
| `MulIPTCRightsTxt` | IPTC rights | IPTC Rechte |
| `MulIPTCStatusTxt` | IPTC status | IPTC Status |
| `MulIPTCObjRefTxt` | IPTC object reference | IPTC Objekt-Referenz |
| `MulIPTCObjTitleTxt` | IPTC object title | IPTC Objekttitel |
| `MulIPTCLastUpdateTxt` / `…MetadataTxt` / `…ExportTxt` | IPTC last update / metadata / export | IPTC letzte Änderung / Metadaten / Export |
| `MulIPTCGrp` | IPTC block | IPTC-Block |
| `MulPhotocreditTxt` | photo credit | Fotocredit |
| `MulSourceTxt` | source | Quelle |
| `MulTypeTxt` / `MulTypeGrp` / `MulTypeVoc` | type | Typ |
| `MulCategoryVoc` | category | Kategorie |
| `MulColorVoc` | colour | Farbe |
| `MulFormatVoc` | format | Format |
| `MulViewVoc` | view | Ansicht |
| `MulMatTechVoc` | material/technique | Material/Technik |
| `MulShootingReasonVoc` | shooting reason | Aufnahmegrund |
| `MulDigitilizationProcessVoc` | digitisation process | Digitalisierungsprozess |
| `MulStatusVoc` / `MulBorrowStatusVoc` | status / loan status | Status / Leihstatus |
| `ApprovalVoc` / `MulApprovalGrp` | approval | Freigabe |
| `LicenceVoc` / `MulRightsGrp` | licence / rights | Lizenz / Rechte |
| `StandardImageBoo` | standard image | Standardbild |
| `ProcessedBoo` | processed | bearbeitet |
| `MulTemplateBoo` | template | Vorlage |
| `ThumbnailBoo` | has thumbnail | Thumbnail vorhanden |
| `TagTxt` / `TagVoc` | tag | Tag |
| `ContentTxt` / `ContentLnu` / `ContentDat` / `ContentVrt` | content | Inhalt |
| `MulObjectRef` | linked object | verknüpftes Objekt |
| `MulPhotographerPerRef` | photographer (Person) | Fotograf:in (Person) |
| `LinkingInfoTxt` / `LinkDisplayTxt` / `LinkStatusVoc` | linking info | Verknüpfungsinfo |
| `MulNotesClb` / `NotesClb` | notes | Notizen |
