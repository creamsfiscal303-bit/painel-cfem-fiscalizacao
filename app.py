                    Processos_distintos=("Processos_distintos", "sum"),
                ).reset_index()
                total = holder_summary["CNPJ"].nunique()
                found = holder_summary[holder_summary["Resultado_conferencia"].str.startswith("Encontrado")]["CNPJ"].nunique()
                missing_count = holder_summary[holder_summary["Resultado_conferencia"].eq("Não localizado no CREA-MS")]["CNPJ"].nunique()
                a, b, c = st.columns(3)
                a.metric("Titulares PJ com CNPJ", f"{total:,}".replace(",", "."))
                b.metric("Localizados no CREA-MS", f"{found:,}".replace(",", "."))
                c.metric("Não localizados", f"{missing_count:,}".replace(",", "."))

                f1, f2, f3, f4 = st.columns(4)
                result_choices = ["Todos"] + sorted(detail["Resultado_conferencia"].dropna().unique().tolist())
                selected_result = f1.selectbox("Resultado da conferência", result_choices, key="crea_match_filter")
                uf_choices = ["Todas"] + sorted(detail["UF"].dropna().astype(str).unique().tolist())
                selected_uf = f2.selectbox("UF", uf_choices, key="crea_match_uf")
                municipality_choices = ["Todos"] + sorted(detail["Município"].dropna().astype(str).unique().tolist())
                selected_municipality = f3.selectbox("Município", municipality_choices, key="crea_match_municipio")
                substance_choices = ["Todas"] + sorted(detail["Substância"].dropna().astype(str).unique().tolist())
                selected_substance = f4.selectbox("Substância mineral", substance_choices, key="crea_match_substancia")

                shown = detail.copy()
                if selected_result != "Todos":
                    shown = shown[shown["Resultado_conferencia"].eq(selected_result)]
                if selected_uf != "Todas":
                    shown = shown[shown["UF"].eq(selected_uf)]
                if selected_municipality != "Todos":
                    shown = shown[shown["Município"].eq(selected_municipality)]
                if selected_substance != "Todas":
                    shown = shown[shown["Substância"].eq(selected_substance)]

                preferred_cols = ["CNPJ", "Nome_ANM", "UF", "Município", "Substância", "Anos_CFEM", "Ultimo_ano_CFEM", "Registros_por_ano", "Registros_CFEM", "Processos_distintos", "Valor_CFEM_R", "Empresa_CREA", "Situacao_CREA", "Resultado_conferencia"]
                display_cols = [c for c in preferred_cols if c in shown.columns]
                st.dataframe(shown[display_cols].sort_values("Valor_CFEM_R", ascending=False), use_container_width=True, hide_index=True, height=500)
                st.caption(f"Exibindo {len(shown):,} combinações de CNPJ, UF, município e substância. Os indicadores acima contam titulares únicos. Os anos e o último ano refletem o período selecionado no filtro global de arrecadação.".replace(",", "."))

                csv_bytes = shown[display_cols].to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "Baixar resultado da conferência (CSV)", csv_bytes,
                    file_name=f"conferencia_crea_ms_cfem_{selected_state.lower()}.csv", mime="text/csv"
                )
                xlsx_buffer = io.BytesIO()
                # Preparar tipos explicitamente antes de gravar: evita que o Excel
                # interprete CNPJ como número e "2022: 12" como hora.
                excel_data = shown[display_cols].copy()
                text_cols = ["CNPJ", "UF", "Município", "Substância", "Anos_CFEM",
                             "Ultimo_ano_CFEM", "Registros_por_ano", "Empresa_CREA",
                             "Situacao_CREA", "Resultado_conferencia"]
                for col in text_cols:
                    if col in excel_data.columns:
                        excel_data[col] = excel_data[col].fillna("").astype(str)
                integer_cols = ["Registros_CFEM", "Processos_distintos"]
                for col in integer_cols:
                    if col in excel_data.columns:
                        excel_data[col] = pd.to_numeric(excel_data[col], errors="coerce").fillna(0).astype("int64")
                if "Valor_CFEM_R" in excel_data.columns:
                    excel_data["Valor_CFEM_R"] = pd.to_numeric(excel_data["Valor_CFEM_R"], errors="coerce")
                with pd.ExcelWriter(xlsx_buffer, engine="openpyxl") as writer:
                    excel_data.to_excel(writer, sheet_name="Conferencia", index=False)
                    ws = writer.sheets["Conferencia"]
                    # Identificadores e campos descritivos ficam explicitamente como texto.
                    for col_name in text_cols:
                        if col_name in display_cols:
                            col_idx = display_cols.index(col_name) + 1
                            for row in range(2, ws.max_row + 1):
                                cell = ws.cell(row=row, column=col_idx)
                                cell.number_format = "@"
                                if cell.value is not None:
                                    cell.value = str(cell.value)
                    # Formatação numérica explícita para contagens e valores monetários.
                    for col_name in integer_cols:
                        if col_name in display_cols:
                            col_idx = display_cols.index(col_name) + 1
                            for row in range(2, ws.max_row + 1):
                                ws.cell(row=row, column=col_idx).number_format = "0"
                    if "Valor_CFEM_R" in display_cols:
                        col_idx = display_cols.index("Valor_CFEM_R") + 1
                        for row in range(2, ws.max_row + 1):
                            ws.cell(row=row, column=col_idx).number_format = '#,##0.00'
                    # Larguras de coluna adequadas para leitura.
                    preferred_widths = {"CNPJ": 18, "UF": 8, "Município": 22, "Substância": 24,
                                        "Anos_CFEM": 22, "Ultimo_ano_CFEM": 18, "Registros_por_ano": 32,
                                        "Registros_CFEM": 16, "Processos_distintos": 20, "Valor_CFEM_R": 18,
                                        "Empresa_CREA": 30, "Situacao_CREA": 18, "Resultado_conferencia": 32}
                    for col_idx, col_name in enumerate(display_cols, start=1):
                        letter = ws.cell(row=1, column=col_idx).column_letter
                        ws.column_dimensions[letter].width = preferred_widths.get(col_name, 18)
                    ws.freeze_panes = "A2"
                    ws.auto_filter.ref = ws.dimensions
                    holder_summary.groupby("Resultado_conferencia", dropna=False).agg(
                        Titulares=("CNPJ", "nunique"),
                        Valor_CFEM_R=("Valor_CFEM_R", "sum"),
                        Processos=("Processos_distintos", "sum")
                    ).reset_index().to_excel(writer, sheet_name="Resumo por titular", index=False)
                st.download_button(
                    "Baixar resultado em Excel", xlsx_buffer.getvalue(),
                    file_name=f"conferencia_crea_ms_cfem_{selected_state.lower()}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
                st.caption("A conferência considera os filtros atualmente aplicados ao painel e somente identificadores com formato de CNPJ. A ausência no cadastro é um indício para análise, não uma conclusão de irregularidade.")
        except Exception as exc:
            st.error("Não foi possível processar a lista de empresas. Confira se o CSV possui CNPJ e nome da empresa.")
            st.caption(str(exc))
    else:
        st.info("Carregue o arquivo EMPRESAS_CREA_MS.csv para iniciar a conferência. O arquivo é processado na sessão e não precisa ser publicado no GitHub.")

with tab_method:
    st.subheader("Como interpretar o painel")
    st.markdown("""
- **Fonte:** arquivo público de arrecadação da CFEM disponibilizado pela ANM.
- **Período:** anos de arrecadação presentes na versão publicada do arquivo (2022 a 2026).
- **Filtro principal:** UF; o painel inicia no Ceará para apoiar o treinamento.
- **Consolidação:** registros são agrupados por identificador disponível e/ou processo; processos diferentes podem pertencer ao mesmo titular.
- **Limitações:** CPF pode estar mascarado; o valor recolhido não é faturamento nem medida direta de produção; registros históricos não comprovam atividade atual.
- **Uso fiscalizatório:** os resultados são indícios para planejamento e seleção de alvos de verificação, não conclusões automáticas de infração.
- **Validação recomendada:** consultar o cadastro da ANM, a situação cadastral do CNPJ, o registro empresarial no CREA competente, as ARTs e a atividade efetivamente executada.
""")
    st.markdown(f"**Arquivo de origem:** [{SOURCE_NAME}]({SOURCE_URL})")

st.caption("Painel demonstrativo. Antes de usar em procedimento oficial, valide amostras dos dados, critérios de triagem e atualidade das informações.")
