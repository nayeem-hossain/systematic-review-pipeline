"""
appraisal.py -- field-driven critical-appraisal instrument selection.

Why this exists: a systematic review needs a named, citable instrument to assess
the risk of bias / methodological quality of its included studies (PRISMA 2020
item 11), and, where applicable, a certainty-of-evidence rating for its outcomes
(items 15, 22). Which instrument is *correct* depends entirely on the review's
field and the design of its primary studies -- Cochrane RoB 2 is right for a
health RCT review and meaningless for a software-engineering benchmark study.
This tool's own worked example is machine-learning intrusion detection, so it
shipped with a single bespoke R/A/T/C rubric and no path to anything else.

This module is a data-only REGISTRY, not a recommendation engine: every domain
name below is transcribed verbatim from the cited source, not paraphrased or
invented, because these become literal data-extraction column headers. Where a
source's full item text could not be independently verified in this project's
research pass, that instrument is marked ``verbatim=False`` and points to the
citation instead of fabricating column text.

Sources are cited inline on each instrument. Nothing here should be trusted
blindly forever -- checklists get revised (ROBINS-I has a 2024 "V2" with 6
domains; this registry uses the original 2016/7-domain version, the one most
existing Cochrane-adjacent SLR tooling still expects). Re-verify before relying
on a citation in a submission.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- core logic: instrument levels ---
# Three different things get appraised in a systematic review, and conflating
# them is a real mistake this registry exists to prevent:
PRIMARY_STUDY = "primary_study"      # per included study -> extraction.csv columns
REVIEW_SELF_CHECK = "review_self_check"  # the SLR itself, once -> a self-appraisal file
CERTAINTY = "certainty"              # per outcome/finding, once each -> GRADE/CERQual rating


@dataclass
class Instrument:
    key: str
    name: str
    citation: str
    url: str
    level: str  # PRIMARY_STUDY | REVIEW_SELF_CHECK | CERTAINTY
    domains: list = field(default_factory=list)   # verbatim item/domain text, in order
    rating_scale: list = field(default_factory=list)
    verbatim: bool = True   # False => domains list is incomplete/paraphrased; see notes
    notes: str = ""


def _slug(text: str, maxlen: int = 40) -> str:
    """Column-header-safe slug from a verbatim question/domain string."""
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:maxlen].rstrip("_")


# ===========================================================================
# INSTRUMENTS -- verbatim domains transcribed from the cited source.
# ===========================================================================
INSTRUMENTS: dict = {

    # --- Software engineering / computer science / cybersecurity ---------------
    "dyba_dingsoyr": Instrument(
        key="dyba_dingsoyr",
        name="Dybå & Dingsøyr (2008) quality-assessment checklist",
        citation="Dyba, T. and Dingsoyr, T. (2008). \"Empirical studies of agile software "
                 "development: A systematic review.\" Information and Software Technology, "
                 "50(9-10), 833-859. doi:10.1016/j.infsof.2008.01.006. Table 3.",
        url="https://doi.org/10.1016/j.infsof.2008.01.006",
        level=PRIMARY_STUDY,
        domains=[
            "Is the paper based on research (or is it merely a \"lessons learned\" "
            "report based on expert opinion)?",
            "Is there a clear statement of the aims of the research?",
            "Is there an adequate description of the context in which the research "
            "was carried out?",
            "Was the research design appropriate to address the aims of the research?",
            "Was the recruitment strategy appropriate to the aims of the research?",
            "Was there a control group with which to compare treatments?",
            "Was the data collected in a way that addressed the research issue?",
            "Was the data analysis sufficiently rigorous?",
            "Has the relationship between researcher and participants been considered "
            "to an adequate degree?",
            "Is there a clear statement of findings?",
            "Is the study of value for research or practice?",
        ],
        rating_scale=["Yes (1)", "Partly (0.5)", "No (0)"],
        notes="The single most widely cited purpose-built quality instrument for "
              "empirical SE/CS studies; the natural default for an ML-IDS or other "
              "empirical-CS review. Items 1-3 are a reporting minimum some reviews use "
              "as an inclusion gate; 4-8 assess rigour; 9-10 credibility; 11 relevance "
              "(the paper's own §3.5 grouping).",
    ),
    "dare": Instrument(
        key="dare",
        name="DARE criteria (Database of Abstracts of Reviews of Effects)",
        citation="Cited in Kitchenham, B. & Charters, S. (2007). \"Guidelines for "
                 "performing Systematic Literature Reviews in Software Engineering\", "
                 "EBSE-2007-01, v2.3, Keele University & University of Durham.",
        url="https://www.york.ac.uk/inst/crd/crddatabases.htm",
        level=REVIEW_SELF_CHECK,
        domains=[
            "Are the review's inclusion and exclusion criteria described and appropriate?",
            "Is the literature search likely to have covered all relevant studies?",
            "Did the reviewers assess the quality/validity of the included studies?",
            "Were the basic data/studies adequately described?",
        ],
        rating_scale=["Yes (1)", "Partly (0.5)", "No / Unknown (0)"],
        notes="Appraises the REVIEW ITSELF, not a primary study -- \"the most "
              "frequently used [instrument]\" for appraising SLRs/tertiary studies in "
              "SE (Baldassarre et al., QAISER, arXiv:2109.10134). Use this as a "
              "one-time self-check of your own review, not as extraction.csv columns.",
    ),
    "garousi_grey_lit": Instrument(
        key="garousi_grey_lit",
        name="Garousi, Felderer & Mäntylä grey-literature quality criteria",
        citation="Garousi, V., Felderer, M., Mantyla, M.V. (2019). \"Guidelines for "
                 "including grey literature and conducting multivocal literature "
                 "reviews in software engineering.\" Information and Software "
                 "Technology, 106, 101-121. doi:10.1016/j.infsof.2018.09.006. Table 7, "
                 "Guideline 11.",
        url="https://doi.org/10.1016/j.infsof.2018.09.006",
        level=PRIMARY_STUDY,
        domains=[
            "Authority of the producer",
            "Methodology",
            "Objectivity",
            "Date",
            "Novelty",
            "Impact",
            "Outlet control",
        ],
        verbatim=True,
        notes="7 category names verbatim from Guideline 11 (each is scored via several "
              "sub-questions in the paper's own Table 7, not reproduced verbatim here). "
              "Use ONLY when the review explicitly includes grey literature -- pair with "
              "dyba_dingsoyr for the formally-published studies in the same review.",
    ),

    # --- Health / clinical / medicine ------------------------------------------
    "rob2": Instrument(
        key="rob2",
        name="Cochrane RoB 2 (risk of bias in randomized trials)",
        citation="Cochrane Handbook, Chapter 8: \"Assessing risk of bias in a "
                 "randomized trial\" (2019 revision).",
        url="https://training.cochrane.org/handbook/current/chapter-08",
        level=PRIMARY_STUDY,
        domains=[
            "Bias arising from the randomization process",
            "Bias due to deviations from intended interventions",
            "Bias due to missing outcome data",
            "Bias in measurement of the outcome",
            "Bias in selection of the reported result",
        ],
        rating_scale=["Low risk", "Some concerns", "High risk"],
        notes="For an individual RANDOMIZED trial (or a specific trial result -- "
              "judgements are made per outcome).",
    ),
    "robins_i": Instrument(
        key="robins_i",
        name="ROBINS-I (risk of bias in non-randomized studies of interventions), "
             "original 2016 version",
        citation="Cochrane Handbook, Chapter 25, Table 25.3.a. Sterne JA et al. (2016). "
                 "BMJ;355:i4919.",
        url="https://www.riskofbias.info/welcome/home/original-2016-version-of-robins-i",
        level=PRIMARY_STUDY,
        domains=[
            "Bias due to confounding",
            "Bias in selection of participants into the study (or into the analysis)",
            "Bias in classification of interventions",
            "Bias due to deviations from intended interventions",
            "Bias due to missing data",
            "Bias in measurement of the outcome",
            "Bias in selection of the reported result",
        ],
        rating_scale=["Low risk", "Moderate risk", "Serious risk", "Critical risk",
                      "No information"],
        notes="For a non-randomized comparative study of an intervention. A newer "
              "ROBINS-I V2 (Nov 2024) consolidates to 6 domains -- this registry uses "
              "the original 7-domain version most existing tooling expects; check "
              "riskofbias.info for V2 if your venue requires it.",
    ),
    "quadas2": Instrument(
        key="quadas2",
        name="QUADAS-2 (diagnostic accuracy studies)",
        citation="Whiting PF et al. (2011). \"QUADAS-2: A Revised Tool for the Quality "
                 "Assessment of Diagnostic Accuracy Studies.\" Ann Intern Med, 155(8), "
                 "529-536.",
        url="https://www.bristol.ac.uk/population-health-sciences/projects/quadas/history/quadas-2/",
        level=PRIMARY_STUDY,
        domains=[
            "Patient selection -- risk of bias",
            "Patient selection -- applicability concerns",
            "Index test -- risk of bias",
            "Index test -- applicability concerns",
            "Reference standard -- risk of bias",
            "Reference standard -- applicability concerns",
            "Flow and timing -- risk of bias",
        ],
        rating_scale=["Low", "High", "Unclear"],
        notes="4 domains (patient selection, index test, reference standard, flow "
              "and timing) each rated for RISK OF BIAS; the first 3 are ALSO rated "
              "separately for APPLICABILITY CONCERNS -- 7 columns total, as above. "
              "For a diagnostic-accuracy study only (e.g. an ML-IDS detector "
              "evaluated as a classifier against ground truth).",
    ),

    # --- Qualitative research (any field) --------------------------------------
    "casp_qualitative": Instrument(
        key="casp_qualitative",
        name="CASP Qualitative Studies Checklist (2024)",
        citation="Critical Appraisal Skills Programme (2024). CASP Qualitative "
                 "Studies Checklist.",
        url="https://casp-uk.net/casp-checklists/CASP-checklist-qualitative-2024.pdf",
        level=PRIMARY_STUDY,
        domains=[
            "Was there a clear statement of the aims of the research?",
            "Is a qualitative methodology appropriate?",
            "Was the research design appropriate to address the aims of the research?",
            "Was the recruitment strategy appropriate to the aims of the research?",
            "Was the data collected in a way that addressed the research issue?",
            "Has the relationship between researcher and participants been "
            "adequately considered?",
            "Have ethical issues been taken into consideration?",
            "Was the data analysis sufficiently rigorous?",
            "Is there a clear statement of findings?",
            "How valuable is the research?",
        ],
        rating_scale=["Yes", "No", "Can't tell"],
        notes="For a qualitative primary study, in any field.",
    ),
    "jbi_qualitative": Instrument(
        key="jbi_qualitative",
        name="JBI Critical Appraisal Checklist for Qualitative Research",
        citation="Joanna Briggs Institute (2020). Checklist for Qualitative Research.",
        url="https://jbi.global/critical-appraisal-tools",
        level=PRIMARY_STUDY,
        domains=[
            "Is there congruity between the stated philosophical perspective and "
            "the research methodology?",
            "Is there congruity between the research methodology and the research "
            "question or objectives?",
            "Is there congruity between the research methodology and the methods "
            "used to collect data?",
            "Is there congruity between the research methodology and the "
            "representation and analysis of data?",
            "Is there congruity between the research methodology and the "
            "interpretation of results?",
            "Is there a statement locating the researcher culturally or theoretically?",
            "Is the influence of the researcher on the research, and vice-versa, "
            "addressed?",
            "Are participants, and their voices, adequately represented?",
            "Is the research ethical according to current criteria or, for recent "
            "studies, is there evidence of ethical approval by an appropriate body?",
            "Do the conclusions drawn in the research report flow from the "
            "analysis, or interpretation, of the data?",
        ],
        rating_scale=["Yes", "No", "Unclear", "Not applicable"],
        notes="For a qualitative primary study; the dominant checklist family in "
              "nursing/allied health. JBI also publishes 12+ other design-specific "
              "checklists (RCT, cohort, case-control, case series, prevalence, "
              "diagnostic accuracy, economic evaluation, quasi-experimental, "
              "text-and-opinion, ...) not reproduced here verbatim -- see the URL.",
    ),

    # --- Mixed methods / social science / education -----------------------------
    "mmat_screening": Instrument(
        key="mmat_screening",
        name="MMAT (2018) screening questions",
        citation="Hong QN et al. (2018). Mixed Methods Appraisal Tool (MMAT), "
                 "Version 2018 -- User Guide. Also: Hong QN et al. (2018), Education "
                 "for Information, 34(4), 285-291. doi:10.3233/EFI-180221.",
        url="http://mixedmethodsappraisaltoolpublic.pbworks.com/",
        level=PRIMARY_STUDY,
        domains=[
            "Are there clear research questions?",
            "Do the collected data allow to address the research questions?",
        ],
        rating_scale=["Yes", "No", "Can't tell"],
        notes="Ask these 2 FIRST, for every study regardless of design. \"Further "
              "appraisal may not be feasible or appropriate when the answer is 'No' "
              "or 'Can't tell' to one or both screening questions.\" Then apply "
              "exactly ONE of the 5 mmat_* category checklists below, matching the "
              "study's actual design.",
    ),
    "mmat_qualitative": Instrument(
        key="mmat_qualitative", name="MMAT (2018) -- Qualitative studies",
        citation="Hong QN et al. (2018), MMAT v2018 User Guide, Part I.1.",
        url="http://mixedmethodsappraisaltoolpublic.pbworks.com/", level=PRIMARY_STUDY,
        domains=[
            "Is the qualitative approach appropriate to answer the research question?",
            "Are the qualitative data collection methods adequate to address the "
            "research question?",
            "Are the findings adequately derived from the data?",
            "Is the interpretation of results sufficiently substantiated by data?",
            "Is there coherence between qualitative data sources, collection, "
            "analysis and interpretation?",
        ],
        rating_scale=["Yes", "No", "Can't tell"],
    ),
    "mmat_rct": Instrument(
        key="mmat_rct", name="MMAT (2018) -- Quantitative randomized controlled trials",
        citation="Hong QN et al. (2018), MMAT v2018 User Guide, Part I.2.",
        url="http://mixedmethodsappraisaltoolpublic.pbworks.com/", level=PRIMARY_STUDY,
        domains=[
            "Is randomization appropriately performed?",
            "Are the groups comparable at baseline?",
            "Are there complete outcome data?",
            "Are outcome assessors blinded to the intervention provided?",
            "Did the participants adhere to the assigned intervention?",
        ],
        rating_scale=["Yes", "No", "Can't tell"],
    ),
    "mmat_nonrandomized": Instrument(
        key="mmat_nonrandomized", name="MMAT (2018) -- Quantitative non-randomized",
        citation="Hong QN et al. (2018), MMAT v2018 User Guide, Part I.3.",
        url="http://mixedmethodsappraisaltoolpublic.pbworks.com/", level=PRIMARY_STUDY,
        domains=[
            "Are the participants representative of the target population?",
            "Are measurements appropriate regarding both the outcome and "
            "intervention (or exposure)?",
            "Are there complete outcome data?",
            "Are the confounders accounted for in the design and analysis?",
            "During the study period, is the intervention administered (or "
            "exposure occurred) as intended?",
        ],
        rating_scale=["Yes", "No", "Can't tell"],
    ),
    "mmat_descriptive": Instrument(
        key="mmat_descriptive", name="MMAT (2018) -- Quantitative descriptive",
        citation="Hong QN et al. (2018), MMAT v2018 User Guide, Part I.4.",
        url="http://mixedmethodsappraisaltoolpublic.pbworks.com/", level=PRIMARY_STUDY,
        domains=[
            "Is the sampling strategy relevant to address the research question?",
            "Is the sample representative of the target population?",
            "Are the measurements appropriate?",
            "Is the risk of nonresponse bias low?",
            "Is the statistical analysis appropriate to answer the research question?",
        ],
        rating_scale=["Yes", "No", "Can't tell"],
    ),
    "mmat_mixed": Instrument(
        key="mmat_mixed", name="MMAT (2018) -- Mixed methods studies",
        citation="Hong QN et al. (2018), MMAT v2018 User Guide, Part I.5.",
        url="http://mixedmethodsappraisaltoolpublic.pbworks.com/", level=PRIMARY_STUDY,
        domains=[
            "Is there an adequate rationale for using a mixed methods design to "
            "address the research question?",
            "Are the different components of the study effectively integrated to "
            "answer the research question?",
            "Are the outputs of the integration of qualitative and quantitative "
            "components adequately interpreted?",
            "Are divergences and inconsistencies between quantitative and "
            "qualitative results adequately addressed?",
            "Do the different components of the study adhere to the quality "
            "criteria of each tradition of the methods involved?",
        ],
        rating_scale=["Yes", "No", "Can't tell"],
    ),
    "eppi_woe": Instrument(
        key="eppi_woe",
        name="EPPI-Centre Weight of Evidence (Gough 2007)",
        citation="Gough, D. (2007). \"Weight of evidence: a framework for the "
                 "appraisal of the quality and relevance of evidence.\" Research "
                 "Papers in Education, 22(2), 213-228. doi:10.1080/02671520701296189.",
        url="https://discovery.ucl.ac.uk/id/eprint/1548262/1/Gough2007Weight213.pdf",
        level=PRIMARY_STUDY,
        domains=[
            "Weight of Evidence A -- generic judgement of the coherence and "
            "integrity of the evidence in its own terms",
            "Weight of Evidence B -- review-specific judgement of the "
            "appropriateness of this research design for answering the review "
            "question (fitness for purpose)",
            "Weight of Evidence C -- review-specific judgement of the relevance of "
            "the study's focus (sample, data, context) to the review question",
            "Weight of Evidence D -- overall judgement combining A, B and C",
        ],
        notes="An alternative to MMAT/CASP for education and applied social-policy "
              "reviews spanning heterogeneous designs; judgements are typically "
              "recorded as free text plus a High/Medium/Low weight per row, not a "
              "fixed rating scale.",
    ),

    # --- Economics ---------------------------------------------------------------
    "drummond": Instrument(
        key="drummond",
        name="Drummond checklist for economic evaluations",
        citation="Drummond, M.F. & Jefferson, T.O. (1996). \"Guidelines for authors "
                 "and peer reviewers of economic submissions to the BMJ.\" BMJ, "
                 "313(7052), 275-283.",
        url="https://www.ispor.org/docs/default-source/euro2024/quality-assessmentdrummond-10-checklist146510-pdf.pdf",
        level=PRIMARY_STUDY,
        domains=[
            "The research question",
            "Description of the study/intervention",
            "Study design",
            "Identification of costs and consequences",
            "Measurement of costs and consequences",
            "Valuation of costs and consequences",
            "Discounting",
            "Incremental analysis",
            "Presentation of results with sensitivity/uncertainty analysis",
            "Discussion of results in the context of policy relevance and "
            "existing literature",
        ],
        notes="For a health-economic or cost-effectiveness evaluation study.",
    ),

    # --- Review-level self-appraisal (the SLR itself, not a primary study) -------
    "amstar2": Instrument(
        key="amstar2",
        name="AMSTAR 2",
        citation="Shea BJ et al. (2017). \"AMSTAR 2: a critical appraisal tool for "
                 "systematic reviews that include randomised or non-randomised "
                 "studies of healthcare interventions, or both.\" BMJ, 358:j4008.",
        url="https://amstar.ca",
        level=REVIEW_SELF_CHECK,
        domains=[
            "1. Did the research questions and inclusion criteria include PICO components?",
            "2. Protocol registered before commencement of the review, with "
            "justification of any deviations [CRITICAL]",
            "3. Did the review explain the selection of study designs?",
            "4. Adequacy of the literature search [CRITICAL]",
            "5. Was study selection performed in duplicate?",
            "6. Was data extraction performed in duplicate?",
            "7. Justification for excluding individual studies [CRITICAL]",
            "8. Adequate description of included studies?",
            "9. Risk of bias from individual studies included in the review [CRITICAL]",
            "10. Reporting on sources of funding of included studies?",
            "11. Appropriateness of meta-analytical methods, if performed [CRITICAL]",
            "12. Assessment of the potential impact of risk of bias on the results?",
            "13. Consideration of risk of bias when interpreting the results [CRITICAL]",
            "14. Satisfactory explanation for any heterogeneity observed?",
            "15. Assessment of presence and likely impact of publication bias "
            "(small-study bias) [CRITICAL]",
            "16. Report of any potential conflicts of interest, including funding?",
        ],
        rating_scale=["Yes", "Partial Yes", "No"],
        verbatim=False,
        notes="Full verbatim wording confirmed for the 7 CRITICAL items only (2, 4, "
              "7, 9, 11, 13, 15) -- see Shea et al. 2017 for the complete original "
              "text of the other 9. Appraises THE REVIEW ITSELF, not a primary "
              "study. Overall confidence: High (no/1 non-critical weakness) / "
              "Moderate (>1 non-critical) / Low (1 critical flaw) / Critically Low "
              "(>1 critical flaw).",
    ),
    "robis": Instrument(
        key="robis",
        name="ROBIS (risk of bias in systematic reviews)",
        citation="Whiting P et al. (2016). \"ROBIS: A new tool to assess risk of "
                 "bias in systematic reviews was developed.\" J Clin Epidemiol, 69, "
                 "225-234.",
        url="https://www.bristol.ac.uk/population-health-sciences/projects/robis/robis-tool/",
        level=REVIEW_SELF_CHECK,
        domains=[
            "Study eligibility criteria",
            "Identification and selection of studies",
            "Data collection and study appraisal",
            "Synthesis and findings",
        ],
        rating_scale=["Low concern", "High concern", "Unclear concern"],
        notes="3-phase process: Phase 1 (optional) assesses relevance; Phase 2 is "
              "the 4 domains above, each via signalling questions; Phase 3 is an "
              "overall low/high/unclear risk-of-bias judgement for the review's "
              "conclusions. Appraises THE REVIEW ITSELF.",
    ),
    "meccir": Instrument(
        key="meccir",
        name="Campbell Standards (MECCIR)",
        citation="Aloe AM, Dewidar O, Hennessy EA, Pigott T, Stewart G, Welch V, "
                 "Wilson DB et al. (2024). \"Campbell Standards: Modernizing "
                 "Campbell's Methodologic Expectations for Campbell Collaboration "
                 "Intervention Reviews (MECCIR).\" Campbell Systematic Reviews, "
                 "20:e1445. doi:10.1002/cl2.1445.",
        url="https://www.campbellcollaboration.org/methods/standards/",
        level=REVIEW_SELF_CHECK,
        domains=[
            "Scope of review",
            "Eligibility criteria",
            "Search strategy",
            "Screening / study selection (with PRISMA flow diagram)",
            "Coding and critical appraisal",
            "Synthesis methods",
            "Discussion / interpretation",
        ],
        verbatim=False,
        notes="7 top-level sections verbatim; ~35 individual items sit beneath them "
              "in the full standard (not reproduced here -- see the citation). Does "
              "NOT hard-mandate a specific primary-study instrument: it requires "
              "authors to \"choose and justify a critical appraisal tool that fits "
              "the purpose of the review.\" Cross-referenced against PRISMA 2020 "
              "and PRISMA-S. Appraises THE REVIEW ITSELF.",
    ),

    # --- Certainty of evidence (per outcome/finding, not per study) --------------
    "grade": Instrument(
        key="grade",
        name="GRADE (certainty of evidence for quantitative findings)",
        citation="GRADE Working Group; Schunemann H, Brozek J, Guyatt G, Oxman A, "
                 "eds. GRADE Handbook. gdt.gradepro.org. On applicability without "
                 "meta-analysis: Murad MH, Mustafa RA, Schunemann HJ, Sultan S, "
                 "Santesso N. (2017) \"Rating the certainty of evidence in the "
                 "absence of a single estimate of effect.\" Evidence-Based "
                 "Medicine, 22(3), 85-87. doi:10.1136/ebmed-2017-110668.",
        url="https://www.gradeworkinggroup.org/",
        level=CERTAINTY,
        domains=[
            "Risk of bias",
            "Inconsistency",
            "Indirectness",
            "Imprecision",
            "Publication bias",
            "(observational studies only, upgrade) Large magnitude of effect",
            "(observational studies only, upgrade) Dose-response gradient",
            "(observational studies only, upgrade) Plausible confounding would "
            "reduce the observed effect",
        ],
        rating_scale=[
            "High -- \"We are very confident that the true effect lies close to "
            "that of the estimate of the effect.\"",
            "Moderate -- \"The true effect is likely to be close to the estimate "
            "of the effect, but there is a possibility that it is substantially "
            "different.\"",
            "Low -- \"The true effect may be substantially different from the "
            "estimate of the effect.\"",
            "Very low -- \"The true effect is likely to be substantially different "
            "from the estimate of effect.\"",
        ],
        notes="Rated PER OUTCOME, starting at High for RCT evidence (Low for "
              "observational evidence) and moving down/up per the domains above. "
              "APPLICABLE WITHOUT META-ANALYSIS -- Murad et al. 2017 is written "
              "specifically for narrative/no-single-estimate synthesis.",
    ),
    "grade_cerqual": Instrument(
        key="grade_cerqual",
        name="GRADE-CERQual (confidence in qualitative synthesis findings)",
        citation="Lewin S et al. (2018). \"Applying GRADE-CERQual to qualitative "
                 "evidence synthesis findings: introduction.\" Implementation "
                 "Science, 13(Suppl 1):2. doi:10.1186/s13012-017-0688-3.",
        url="https://www.cerqual.org/",
        level=CERTAINTY,
        domains=[
            "Methodological limitations",
            "Coherence",
            "Adequacy of data",
            "Relevance",
        ],
        rating_scale=[
            "High -- \"It is highly likely that the review finding is a reasonable "
            "representation of the phenomenon of interest.\"",
            "Moderate -- \"It is likely that the review finding is a reasonable "
            "representation of the phenomenon of interest.\"",
            "Low -- \"It is possible that the review finding is a reasonable "
            "representation of the phenomenon of interest.\"",
            "Very low -- \"It is not clear whether the review finding is a "
            "reasonable representation of the phenomenon of interest.\"",
        ],
        notes="Rated PER REVIEW FINDING (an analytic output of a qualitative "
              "evidence synthesis), not per primary study.",
    ),
}


def instrument_columns(instrument_key: str) -> list:
    """Extraction-sheet column names for one instrument's domains, namespaced and
    slugged so two instruments never collide on a header."""
    inst = INSTRUMENTS[instrument_key]
    out = []
    for i, domain in enumerate(inst.domains, start=1):
        out.append(f"{instrument_key}__{i}_{_slug(domain)}")
    return out


def render_review_self_appraisal(instrument_key: str) -> str:
    """A fillable markdown checklist for a REVIEW_SELF_CHECK instrument (AMSTAR 2,
    ROBIS, DARE, MECCIR) -- one row per verbatim domain, rating and justification
    left blank for a human to complete against the finished manuscript.

    Primary-study instruments get real columns in extraction.csv via
    instrument_columns(); a review-level instrument has no per-study rows to
    attach to (it appraises the review once, not each included study), so this
    is the parallel artifact for that different kind of object.
    """
    inst = INSTRUMENTS[instrument_key]
    if inst.level != REVIEW_SELF_CHECK:
        raise ValueError(
            f"{instrument_key!r} is a {inst.level} instrument, not a "
            f"review-self-check instrument -- use instrument_columns() for "
            f"primary-study instruments instead."
        )

    lines = [
        f"# Review self-appraisal -- {inst.name}",
        "",
        f"**Citation:** {inst.citation}",
        f"**Source:** {inst.url}",
        "",
        "This checklist appraises the conduct of the review itself, not any "
        "included primary study. Fill in Rating and Justification for every "
        "domain against the finished manuscript -- a checklist that is all "
        "the best rating on a first pass is a sign it's being graded against "
        "an aspirational review, not the one actually written.",
        "",
    ]
    if not inst.verbatim:
        lines += [
            f"> **Note:** {inst.notes}",
            "",
        ]
    if inst.rating_scale:
        lines.append(f"**Rating scale:** {', '.join(inst.rating_scale)}")
        lines.append("")

    lines += ["| # | Domain | Rating | Justification |", "|---|---|---|---|"]
    for i, domain in enumerate(inst.domains, start=1):
        cell = domain.replace("|", "\\|")
        lines.append(f"| {i} | {cell} | | |")

    return "\n".join(lines) + "\n"


# ===========================================================================
# FIELD PROFILES -- what to recommend when a user names their review's field.
# ===========================================================================
@dataclass
class FieldProfile:
    key: str
    label: str
    primary_study_instruments: list          # instrument keys; user picks/confirms
    certainty_framework: str                  # instrument key, or "" if none established
    review_level_instrument: str = ""         # instrument key, or ""
    justification: str = ""                   # required when an instrument is "none"/absent


FIELD_PROFILES: dict = {
    "software_engineering": FieldProfile(
        key="software_engineering",
        label="Software engineering / computer science / cybersecurity",
        primary_study_instruments=["dyba_dingsoyr"],
        certainty_framework="",
        review_level_instrument="dare",
        justification="Software engineering has no established certainty-of-evidence "
                      "framework. GRADE is used in SE, but inconsistently: Santos et al. "
                      "(2025, Empirical Software Engineering, doi:10.1007/s10664-025-10728-9) "
                      "found only 22 SE papers claiming a formal strength-of-evidence "
                      "assessment, GRADE the most common among them, and conclude "
                      "adoption is \"not faithful to original GRADE guidance.\" This "
                      "review therefore reports quality via Dyba & Dingsoyr (2008) "
                      "without a certainty-of-evidence layer, which is the field's own "
                      "documented practice, not a gap unique to this tool.",
    ),
    "health_rct": FieldProfile(
        key="health_rct",
        label="Health / clinical / medicine -- randomized trials",
        primary_study_instruments=["rob2"],
        certainty_framework="grade",
        review_level_instrument="amstar2",
    ),
    "health_nonrandomized": FieldProfile(
        key="health_nonrandomized",
        label="Health / clinical / medicine -- non-randomized studies",
        primary_study_instruments=["robins_i"],
        certainty_framework="grade",
        review_level_instrument="amstar2",
    ),
    "health_diagnostic": FieldProfile(
        key="health_diagnostic",
        label="Health / clinical / medicine -- diagnostic accuracy studies",
        primary_study_instruments=["quadas2"],
        certainty_framework="grade",
        review_level_instrument="amstar2",
        justification="GRADE has a diagnostic-test-accuracy-specific extension beyond "
                      "the 5 generic domains implemented here; consult the GRADE "
                      "Working Group's diagnostic guidance directly for a submission "
                      "in this sub-field.",
    ),
    "qualitative_research": FieldProfile(
        key="qualitative_research",
        label="Qualitative research (any field)",
        primary_study_instruments=["casp_qualitative"],
        certainty_framework="grade_cerqual",
    ),
    "social_science_education": FieldProfile(
        key="social_science_education",
        label="Social science / education / psychology",
        primary_study_instruments=["mmat_screening"],
        certainty_framework="grade_cerqual",
        review_level_instrument="meccir",
        justification="MMAT requires selecting one of 5 design-specific checklists "
                      "(mmat_qualitative / mmat_rct / mmat_nonrandomized / "
                      "mmat_descriptive / mmat_mixed) per included study, after the "
                      "2 screening questions above; the wizard asks which apply.",
    ),
    "nursing_allied_health": FieldProfile(
        key="nursing_allied_health",
        label="Nursing / allied health",
        primary_study_instruments=["jbi_qualitative"],
        certainty_framework="grade_cerqual",
        justification="JBI publishes 12+ design-specific checklists (RCT, cohort, "
                      "case-control, prevalence, diagnostic accuracy, economic "
                      "evaluation, ...); only the qualitative-research checklist's "
                      "full text was verified for this registry. For a non-"
                      "qualitative design, consult jbi.global directly.",
    ),
    "environmental_ecology": FieldProfile(
        key="environmental_ecology",
        label="Environmental science / ecology / conservation",
        primary_study_instruments=["robins_i"],
        certainty_framework="",
        justification="The Collaboration for Environmental Evidence (CEE) has its own "
                      "Critical Appraisal Tool (environmentalevidence.org/cee-critical-"
                      "appraisal-tool/), built because \"there are currently no such "
                      "critical appraisal tools in environmental management\" comparable "
                      "to RoB2/ROBINS-I (Konno et al. 2022, Environmental Evidence, "
                      "doi:10.1186/s13750-022-00264-0) -- but this registry could not "
                      "independently verify CEE's exact domain wording, so it defaults "
                      "to ROBINS-I, the instrument CEE's own tool is explicitly modelled "
                      "on, and recommends consulting the CEE tool directly before "
                      "submission. Use ROSES (environmentalevidence.org/roses/) for "
                      "reporting, alongside or instead of PRISMA.",
    ),
    "management_business": FieldProfile(
        key="management_business",
        label="Management / business",
        primary_study_instruments=["casp_qualitative", "mmat_screening"],
        certainty_framework="",
        justification="Management has no dominant field-specific appraisal checklist. "
                      "Tranfield, Denyer & Smart (2003, British Journal of Management, "
                      "14(3), 207-222) prescribe a systematic REVIEW PROCESS, not a "
                      "named appraisal instrument; management reviews typically borrow "
                      "CASP, MMAT, or JBI checklists per included study design.",
    ),
    "economics": FieldProfile(
        key="economics",
        label="Economics (economic evaluations)",
        primary_study_instruments=["drummond"],
        certainty_framework="",
    ),
    "engineering_other": FieldProfile(
        key="engineering_other",
        label="Engineering (non-software: civil / mechanical / electrical / other)",
        primary_study_instruments=["dyba_dingsoyr"],
        certainty_framework="",
        justification="No established discipline-specific SLR appraisal standard "
                      "exists for non-software engineering fields; reviews in these "
                      "fields typically adapt Kitchenham's software-engineering "
                      "guidelines (Kitchenham & Charters 2007) or a generic checklist. "
                      "This review uses Dyba & Dingsoyr (2008) as that adapted "
                      "instrument -- state this adaptation explicitly in your methods.",
    ),
    "generic_other": FieldProfile(
        key="generic_other",
        label="Other / not listed",
        primary_study_instruments=[],
        certainty_framework="",
        justification="No field was matched to a researched instrument. Select an "
                      "instrument manually from the full registry (srp.appraisal."
                      "INSTRUMENTS) based on your primary studies' actual design, or "
                      "record why none was applied.",
    ),
}


def compose_appraisal_disclosure(field_key: str, chosen_primary: list,
                                  chosen_certainty: str, chosen_review_level: str) -> str:
    """A ready-to-paste paragraph justifying the appraisal choice, for
    PROVENANCE.md / a methods section -- so PRISMA items 11/15/22 get an answer
    (possibly "not applicable, because...") instead of silence.
    """
    profile = FIELD_PROFILES.get(field_key)
    lines = []
    if profile is None:
        return ("No research field was recorded for this review, so no "
                "field-appropriate appraisal instrument was auto-selected.")

    if chosen_primary:
        names = ", ".join(INSTRUMENTS[k].name for k in chosen_primary if k in INSTRUMENTS)
        lines.append(f"Included studies were critically appraised using {names}, "
                      f"the instrument(s) recommended for {profile.label} reviews.")
    else:
        lines.append(f"No primary-study appraisal instrument was selected for this "
                      f"{profile.label} review.")

    if chosen_certainty:
        inst = INSTRUMENTS.get(chosen_certainty)
        if inst:
            lines.append(f"Certainty of evidence was assessed using {inst.name}.")
    elif profile.justification:
        lines.append(profile.justification)

    if chosen_review_level:
        inst = INSTRUMENTS.get(chosen_review_level)
        if inst:
            lines.append(f"This review's own conduct was self-appraised against "
                          f"{inst.name}.")

    return " ".join(lines)
